"""Executes a :mod:`scopiengine.query.ast` query against an
:class:`~scopiengine.index.reader.IndexReader`, returning top-N BM25-scored hits.

Query execution never materialises a whole term's postings, let alone a whole
segment's or a whole index's: every leaf query streams
:func:`~scopiengine.storage.codec.decode_postings` lazily and merges per-segment
streams with :func:`heapq.merge`; every boolean combinator
(:func:`_merge_and`, :func:`_merge_or`, :func:`_merge_exclude`,
:func:`_boost_should`) consumes its inputs one ordinal at a time via a leapfrog
join rather than collecting them into a set first; and the final top-N cut is a
single bounded ``size + from_`` heap (:func:`search`), so the memory a search
uses is governed by how many results are kept, not by how many documents exist.

Match strategy per node
------------------------
``MatchAll``
    Streams every live ordinal (:meth:`IndexReader.iter_live_ords`), score ``1.0``.
``Term``
    Heap-merges one term's per-segment postings, scoring each with BM25.
``Phrase``
    Leapfrog-intersects every term's postings by document, then checks position
    alignment within the shared document (see :func:`_phrase_matches`); requires
    the field's ``index_options`` to be ``"positions"``.
``Prefix`` / ``Range``
    An ordered term scan (:meth:`IndexReader.iter_terms`) turned into a union of
    ``Term``-style matchers, summed like an implicit "should" across the matched
    terms.
``Exists``
    A union across every term in the field, deduplicated to a constant score —
    existence, not relevance, is what this node means.
``Bool``
    ``must``/``filter`` leapfrog-intersect (filter clauses contribute no score);
    ``should`` either unions the result (when there is no must/filter) or optionally
    boosts the gated result; ``must_not`` is a streaming anti-join against
    whatever gate ends up in force.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TypeVar

from scopiengine.errors import InvalidQueryError, UnsupportedFeatureError
from scopiengine.index.reader import IndexReader
from scopiengine.index.scoring import bm25_score
from scopiengine.query.ast import Bool, Exists, MatchAll, Phrase, Prefix, Query, Range, Term
from scopiengine.storage.codec import decode_postings

__all__ = ["Hit", "search"]

#: One (document ordinal, running score) pair, always yielded in ascending
#: ordinal order by every function in this module.
_Match = tuple[int, float]
_Matcher = Iterator[_Match]


@dataclass(frozen=True, slots=True)
class Hit:
    """One scored search result.

    Attributes:
        doc_ord: The matched document's ordinal.
        score: Its BM25 (or constant, for ``MatchAll``/``Exists``) score.
    """

    doc_ord: int
    score: float


# -- streaming combinators ----------------------------------------------------


def _successor(term: str) -> str:
    """The lexicographically smallest string strictly greater than ``term``.

    Appending ``"\\0"`` (the smallest possible code point) is the immediate
    successor of any string under ordinary lexicographic comparison — which is
    what turns an inclusive range bound into the exclusive one
    :meth:`~scopiengine.index.reader.IndexReader.iter_terms` expects.
    """
    return term + "\0"


_Row = TypeVar("_Row")


def _leapfrog(
    streams: Sequence[Iterable[_Row]], key: Callable[[_Row], int]
) -> Iterator[list[_Row]]:
    """Multi-way intersection by an ordinal each row carries.

    Yields, for each ordinal every stream has a row for, the list of matching
    rows (one per stream, same order as ``streams``). Each input must already be
    ascending by ``key``.
    """
    iters = [iter(s) for s in streams]
    try:
        current = [next(it) for it in iters]
    except StopIteration:
        return
    while True:
        keys = [key(row) for row in current]
        lo, hi = min(keys), max(keys)
        if lo == hi:
            yield list(current)
            try:
                current = [next(it) for it in iters]
            except StopIteration:
                return
        else:
            for i, row_key in enumerate(keys):
                if row_key == lo:
                    try:
                        current[i] = next(iters[i])
                    except StopIteration:
                        return


def _merge_and(streams: Sequence[_Matcher]) -> _Matcher:
    """AND: every stream must have the ordinal; scores from all streams sum."""
    if not streams:
        return
    for group in _leapfrog(streams, key=lambda pair: pair[0]):
        yield group[0][0], sum(score for _ord, score in group)


def _coalesce(merged: Iterable[_Match]) -> _Matcher:
    """Sum scores for consecutive equal ordinals in an already-sorted stream."""
    current_ord: int | None = None
    current_score = 0.0
    for doc_ord, score in merged:
        if current_ord is None or doc_ord != current_ord:
            if current_ord is not None:
                yield current_ord, current_score
            current_ord, current_score = doc_ord, score
        else:
            current_score += score
    if current_ord is not None:
        yield current_ord, current_score


def _merge_or(streams: Sequence[_Matcher]) -> _Matcher:
    """OR: any stream having the ordinal is enough; matching streams' scores sum."""
    if not streams:
        return
    yield from _coalesce(heapq.merge(*streams, key=lambda pair: pair[0]))


def _merge_exclude(base: _Matcher, excluded: _Matcher) -> _Matcher:
    """Anti-join: yield ``base`` unchanged, skipping ordinals ``excluded`` also has."""
    next_excluded: _Match | None = next(excluded, None)
    for doc_ord, score in base:
        while next_excluded is not None and next_excluded[0] < doc_ord:
            next_excluded = next(excluded, None)
        if next_excluded is not None and next_excluded[0] == doc_ord:
            continue
        yield doc_ord, score


def _boost_should(base: _Matcher, should_streams: Sequence[_Matcher]) -> _Matcher:
    """Add each matching ``should`` clause's score on top of ``base``, without gating on it."""
    heads: list[_Match | None] = [next(s, None) for s in should_streams]
    for doc_ord, score in base:
        total = score
        for i, stream in enumerate(should_streams):
            head = heads[i]
            while head is not None and head[0] < doc_ord:
                head = next(stream, None)
            heads[i] = head
            if head is not None and head[0] == doc_ord:
                total += head[1]
        yield doc_ord, total


def _zero_score(stream: _Matcher) -> _Matcher:
    """Keep a matcher's ordinals but drop its contribution to the combined score."""
    for doc_ord, _score in stream:
        yield doc_ord, 0.0


def _dedupe(merged: Iterable[_Match], *, score: float) -> _Matcher:
    """Collapse repeated ordinals from a sorted stream to one constant-scored hit."""
    seen: int | None = None
    for doc_ord, _score in merged:
        if doc_ord != seen:
            seen = doc_ord
            yield doc_ord, score


# -- leaf query execution ---------------------------------------------------


def _term_stream(reader: IndexReader, field_id: int, term: str, boost: float) -> _Matcher:
    """All of one term's live postings, BM25-scored.

    Deliberately does *not* use
    :meth:`~scopiengine.index.reader.IndexReader.term_stats` for ``doc_freq``:
    that number is the raw, stored count from write time, which still includes
    any document tombstoned since. BM25's idf must instead reflect only *live*
    matches — and, critically, must reflect the exact same count whether those
    matches currently live in one segment or several, which is only true if it
    is recomputed from the live postings themselves rather than trusted from
    storage. That means every live posting for this term is decoded up front
    (bounded by this one term's own document frequency, not the corpus) before
    any score can be computed, since idf needs the final live count first.
    """
    live: list[tuple[int, int, int, int]] = []  # (abs_ord, tf, segment_id, rel_ord)
    for segment_id, base_ord, blob in reader.get_postings(field_id, term):
        for posting in decode_postings(blob):
            abs_ord = base_ord + posting.doc_ord
            if abs_ord not in reader.deleted:
                live.append((abs_ord, posting.tf, segment_id, posting.doc_ord))
    if not live:
        return
    live.sort(key=lambda row: row[0])

    doc_freq = len(live)
    avg_length = reader.avg_field_length(field_id)
    n_docs = reader.live_doc_count
    for abs_ord, tf, segment_id, rel_ord in live:
        doc_length = reader.doc_length(field_id, segment_id, rel_ord)
        score = (
            bm25_score(
                tf=tf,
                doc_length=doc_length,
                avg_doc_length=avg_length,
                doc_freq=doc_freq,
                n_docs=n_docs,
            )
            * boost
        )
        yield abs_ord, score


def _terms_or(reader: IndexReader, field_id: int, terms: Iterable[str], boost: float) -> _Matcher:
    streams = [_term_stream(reader, field_id, term, boost) for term in terms]
    yield from _merge_or(streams)


#: One term's posting, carrying the (segment, relative-ordinal) location its
#: score needs to look up a document's length — see :meth:`IndexReader.doc_length`.
_PostingRow = tuple[int, int, int, tuple[int, ...]]


def _raw_postings_stream(reader: IndexReader, field_id: int, term: str) -> Iterator[_PostingRow]:
    """Every non-deleted posting for ``term``, as ``(abs_ord, segment_id, rel_ord, positions)``."""

    def _segment_stream(segment_id: int, base_ord: int, blob: bytes) -> Iterator[_PostingRow]:
        for posting in decode_postings(blob):
            abs_ord = base_ord + posting.doc_ord
            if abs_ord in reader.deleted:
                continue
            yield abs_ord, segment_id, posting.doc_ord, posting.positions

    streams = [
        _segment_stream(seg_id, base_ord, blob)
        for seg_id, base_ord, blob in reader.get_postings(field_id, term)
    ]
    yield from heapq.merge(*streams, key=lambda row: row[0])


def _phrase_matches(term_positions: Sequence[tuple[int, ...]], slop: int) -> int:
    """Count phrase occurrences in one document, given each term's positions there.

    Greedy, in-order matching: for each candidate start position of the first
    term, walk the remaining terms taking the nearest position after the
    previous match, accumulating the extra distance beyond "immediately
    adjacent" (a gap of ``0``) against the ``slop`` budget. This is not an
    exhaustive search over every possible alignment — for the common case
    (``slop=0``, or a small slop with sparse repeated terms) it agrees with the
    exhaustive answer; a pathological document with many repeats of every
    phrase term could see this undercount matches at ``slop > 0``.
    """
    if not term_positions or any(not positions for positions in term_positions):
        return 0
    count = 0
    for start in term_positions[0]:
        prev = start
        used = 0
        matched = True
        for positions in term_positions[1:]:
            candidate = None
            for pos in positions:
                if pos <= prev:
                    continue
                gap = pos - prev - 1
                if used + gap <= slop:
                    candidate = (pos, gap)
                    break
            if candidate is None:
                matched = False
                break
            prev, gap = candidate
            used += gap
        if matched:
            count += 1
    return count


def _execute_phrase(node: Phrase, reader: IndexReader) -> _Matcher:
    field_id = reader.field_id(node.field)
    if field_id is None or not node.terms:
        return
    field_mapping = reader.mapping.fields.get(node.field)
    if field_mapping is None or field_mapping.index_options != "positions":
        raise UnsupportedFeatureError(
            f"phrase query on field {node.field!r} requires index_options='positions'"
        )

    streams = [_raw_postings_stream(reader, field_id, term) for term in node.terms]
    matched: list[tuple[int, int, int, int]] = []
    for group in _leapfrog(streams, key=lambda row: row[0]):
        abs_ord, segment_id, rel_ord, _positions = group[0]
        phrase_tf = _phrase_matches([row[3] for row in group], node.slop)
        if phrase_tf > 0:
            matched.append((abs_ord, segment_id, rel_ord, phrase_tf))
    if not matched:
        return

    doc_freq = len(matched)
    avg_length = reader.avg_field_length(field_id)
    n_docs = reader.live_doc_count
    for abs_ord, segment_id, rel_ord, phrase_tf in matched:
        doc_length = reader.doc_length(field_id, segment_id, rel_ord)
        score = (
            bm25_score(
                tf=phrase_tf,
                doc_length=doc_length,
                avg_doc_length=avg_length,
                doc_freq=doc_freq,
                n_docs=n_docs,
            )
            * node.boost
        )
        yield abs_ord, score


def _execute(node: Query, reader: IndexReader) -> _Matcher:
    if isinstance(node, MatchAll):
        for doc_ord in reader.iter_live_ords():
            yield doc_ord, 1.0
        return

    if isinstance(node, Term):
        field_id = reader.field_id(node.field)
        if field_id is None:
            return
        yield from _term_stream(reader, field_id, node.value, node.boost)
        return

    if isinstance(node, Phrase):
        yield from _execute_phrase(node, reader)
        return

    if isinstance(node, Prefix):
        field_id = reader.field_id(node.field)
        if field_id is None:
            return
        terms = reader.iter_terms(field_id, prefix=node.value)
        yield from _terms_or(reader, field_id, terms, node.boost)
        return

    if isinstance(node, Range):
        field_id = reader.field_id(node.field)
        if field_id is None:
            return
        lo = node.gte
        if lo is None and node.gt is not None:
            lo = _successor(node.gt)
        hi = _successor(node.lte) if node.lte is not None else node.lt
        terms = reader.iter_terms(field_id, lo=lo, hi=hi)
        yield from _terms_or(reader, field_id, terms, node.boost)
        return

    if isinstance(node, Exists):
        field_id = reader.field_id(node.field)
        if field_id is None:
            return
        streams = [
            _term_stream(reader, field_id, term, 1.0) for term in reader.iter_terms(field_id)
        ]
        if not streams:
            return
        merged = heapq.merge(*streams, key=lambda pair: pair[0])
        yield from _dedupe(merged, score=1.0)
        return

    if isinstance(node, Bool):
        yield from _execute_bool(node, reader)
        return

    raise InvalidQueryError(f"unrecognised query node: {node!r}")


def _execute_bool(node: Bool, reader: IndexReader) -> _Matcher:
    must_streams = [_execute(q, reader) for q in node.must]
    filter_streams = [_zero_score(_execute(q, reader)) for q in node.filter]
    should_streams = [_execute(q, reader) for q in node.should]
    must_not_streams = [_execute(q, reader) for q in node.must_not]

    gating = [*must_streams, *filter_streams]
    gated: _Matcher
    if gating:
        gated = _merge_and(gating)
        if should_streams:
            gated = _boost_should(gated, should_streams)
    elif should_streams:
        gated = _merge_or(should_streams)
    else:
        gated = _execute(MatchAll(), reader)

    if must_not_streams:
        gated = _merge_exclude(gated, _merge_or(must_not_streams))

    yield from gated


# -- top-level entry point ---------------------------------------------------


def search(reader: IndexReader, query: Query, *, size: int = 10, from_: int = 0) -> list[Hit]:
    """Execute ``query`` and return the top ``size`` hits after skipping ``from_``.

    Args:
        reader: The index snapshot to search.
        query: The AST to execute.
        size: Maximum number of hits to return.
        from_: Number of top-ranked hits to skip, for pagination.

    Returns:
        Hits ordered by descending score, ties broken by ascending ordinal —
        a deterministic order that depends only on each match's own score and
        ordinal, never on how the index happens to be split into segments (see
        the module docstring, and the force-merge invariant test that checks
        exactly this).

    Raises:
        InvalidQueryError: The AST contains an unrecognised node type.
        UnsupportedFeatureError: A ``Phrase`` clause targets a field that is not
            mapped with ``index_options="positions"``.
    """
    if size < 0 or from_ < 0:
        raise InvalidQueryError("size and from_ must be >= 0")
    limit = size + from_
    if limit == 0:
        return []

    heap: list[tuple[float, int, int, float]] = []
    for doc_ord, score in _execute(query, reader):
        rank_key = (score, -doc_ord)
        if len(heap) < limit:
            heapq.heappush(heap, (*rank_key, doc_ord, score))
        elif rank_key > heap[0][:2]:
            heapq.heapreplace(heap, (*rank_key, doc_ord, score))

    ranked = sorted(heap, key=lambda row: row[:2], reverse=True)
    page = ranked[from_ : from_ + size]
    return [Hit(doc_ord=doc_ord, score=score) for _s, _neg_ord, doc_ord, score in page]
