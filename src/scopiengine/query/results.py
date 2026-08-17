"""The Elasticsearch-shaped response envelope, and the two entry points — one
per query language — that build it end to end: run the AST, fetch the page's
source documents, optionally re-sort/project/aggregate, and render.

``run_scopiql`` and ``run_dsl`` are what :mod:`scopiengine.api` and the
``scopi search`` CLI command both call, so the CLI (no server) and the REST
API produce byte-identical envelopes for the same query.

Envelope shape
--------------
::

    {
      "took": <ms, rounded>,
      "timed_out": false,
      "hits": {
        "total": {"value": <int>, "relation": "eq"},
        "max_score": <float | null>,
        "hits": [{"_index": ..., "_id": ..., "_score": ..., "_source": {...}}, ...]
      },
      "aggregations": {"<name>": {"buckets": [...], ...}},   # only when requested
      "scopi": {
        "query": {...},           # the compiled AST, rendered as JSON (ast_to_dict)
        "segments_touched": <int>,   # live segments the search scanned
        "took_ms": <float, unrounded>
      }
    }

``hits.total.value`` is always the true match count (see
:class:`scopiengine.index.searcher.SearchResult`), never the page length —
regardless of whether a field sort had to truncate (see below).

Sort: index-wide, not page-local
----------------------------------
``sort`` (ScopiQL's ``| sort`` stage, or the DSL's ``sort``) on a real field
is a genuine index-wide sort, not a re-sort of a relevance-ranked page: a
pure ``_score`` sort (the default when there is no ``sort`` at all) is exact
and unbounded, computed inline while matching, so it keeps the original
single-pass fast path. Any *field* sort instead streams every match
(:func:`~scopiengine.index.searcher.iter_matches`), fetches each candidate's
sort key from its stored source in batches, and folds candidates into a
bounded min-heap of the best ``from_ + size`` seen so far — so memory scales
with the page size, never with the number of matches, while the result is
still the true top-N by that field, not an artifact of which documents
happened to rank highest by relevance.

That candidate scan stops after
:attr:`~scopiengine.settings.Settings.max_sort_candidates` matches (default
``10000``) — past that point, a field sort is no longer guaranteed exact.
When it truncates, ``total`` still reports the true, complete match count
(counting continues after the cap, just without the per-candidate source
fetch), but the response's ``scopi.sort_truncated`` is set to ``true`` (with
``scopi.max_sort_candidates`` alongside it) so a truncated sort is never
silently indistinguishable from a complete one, and a warning is logged
naming the setting. See ``docs/QUERY_LANGUAGE.md`` for the full picture,
including why a *page-local* re-sort was rejected: for a filter-shaped query
like ``level:ERROR``, BM25 relevance is close to meaningless, so "the top N
by relevance, then sorted among themselves" silently answers a different
question than "the N most recent."
"""

from __future__ import annotations

import heapq
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from scopiengine.index.reader import IndexReader
from scopiengine.index.searcher import Hit, iter_matches
from scopiengine.logging_conf import get_logger
from scopiengine.mapping.mapping import Mapping
from scopiengine.query.ast import Bool, Exists, MatchAll, Phrase, Prefix, Query, Range, Term
from scopiengine.query.dsl import TermsAgg, compile_dsl
from scopiengine.query.scopiql import SortSpec
from scopiengine.query.scopiql import parse as parse_scopiql

_logger = get_logger(__name__)

if TYPE_CHECKING:
    # Deferred to a type-checking-only import: `Engine` sits above this module
    # in the dependency graph (it builds on the query layer, not the other way
    # around), and `scopiengine.index.manager` — which `Engine` itself pulls
    # in — imports `scopiengine.query.ast`, which imports this package. A
    # module-level `from scopiengine.engine import Engine` here would close
    # that into a real circular import; `from __future__ import annotations`
    # (top of file) means every annotation below is a string at runtime, so
    # this guarded import is all a type checker needs.
    from scopiengine.engine import Engine

__all__ = ["ast_to_dict", "run_dsl", "run_scopiql", "terms_aggregation"]


def ast_to_dict(node: Query) -> dict[str, Any]:
    """Render a query AST node as a JSON-friendly nested dict.

    Used for the ``scopi.query`` debug block in every search response, and by
    ``scopi search --explain``.
    """
    if isinstance(node, MatchAll):
        return {"match_all": {}}
    if isinstance(node, Term):
        return {"term": {"field": node.field, "value": node.value, "boost": node.boost}}
    if isinstance(node, Phrase):
        return {
            "phrase": {
                "field": node.field,
                "terms": list(node.terms),
                "slop": node.slop,
                "boost": node.boost,
            }
        }
    if isinstance(node, Prefix):
        return {"prefix": {"field": node.field, "value": node.value, "boost": node.boost}}
    if isinstance(node, Range):
        return {
            "range": {
                "field": node.field,
                "gte": node.gte,
                "gt": node.gt,
                "lte": node.lte,
                "lt": node.lt,
                "boost": node.boost,
            }
        }
    if isinstance(node, Exists):
        return {"exists": {"field": node.field}}
    if isinstance(node, Bool):
        return {
            "bool": {
                "must": [ast_to_dict(q) for q in node.must],
                "should": [ast_to_dict(q) for q in node.should],
                "must_not": [ast_to_dict(q) for q in node.must_not],
                "filter": [ast_to_dict(q) for q in node.filter],
            }
        }
    raise TypeError(f"unrecognised query node: {node!r}")  # pragma: no cover - exhaustive above


def _extract_field(doc: dict[str, Any], field: str) -> Any:
    """Look up a dotted field path in a (possibly nested) document.

    A field named ``foo.keyword`` looks up ``foo`` instead — the document's
    stored source never has the dynamic-mapping ``.keyword`` sub-field as an
    actual JSON key, only the mapping does. A multi-valued (list) leaf yields
    its first element, matching how a single "value" is expected for sorting
    and bucketing.
    """
    path = field[: -len(".keyword")] if field.endswith(".keyword") else field
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, list):
        return node[0] if node else None
    return node


def _set_nested(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _project_source(doc: dict[str, Any], source: bool | tuple[str, ...]) -> dict[str, Any] | None:
    """Filter a document's source per a DSL ``_source``/ScopiQL ``fields`` spec.

    ``True`` (the default) keeps the whole document; ``False`` omits
    ``_source`` from the hit entirely (returns ``None``); a tuple projects
    just those field paths, rebuilding their nesting.
    """
    if source is False:
        return None
    if source is True:
        return doc
    projected: dict[str, Any] = {}
    for path in source:
        value = _extract_field(doc, path)
        if value is None:
            continue
        _set_nested(projected, path, value)
    return projected


def _needs_index_wide_sort(sort: tuple[SortSpec, ...]) -> bool:
    """Whether ``sort`` requires :func:`_stream_sorted_hits`.

    A pure ``_score`` *descending* sort — explicit, or the default when
    ``sort`` is empty — is already exactly what a normal relevance search
    returns, so it keeps the original, unbounded, single-pass fast path
    untouched. Anything else (any real field, ``_score`` ascending, or a
    multi-key sort) needs the bounded index-wide path to be correct.
    """
    if not sort:
        return False
    return not (len(sort) == 1 and sort[0].field == "_score" and sort[0].descending)


@dataclass(frozen=True, slots=True)
class _RankEntry:
    """One sort candidate's extracted key, ready for the bounded top-N heap.

    Attributes:
        keyvals: One extracted value per :class:`SortSpec` in ``sort``, aligned by index.
        missing: Parallel to ``keyvals``: whether that value was absent.
        doc_ord: The candidate's document ordinal.
        score: Its relevance score — carried through so a field-sorted
            response can still show ``_score``, the way Elasticsearch does
            with ``track_scores``.
        sort: The sort specs ``keyvals``/``missing`` are aligned to — shared
            by reference across every entry in one sort, not copied.
    """

    keyvals: tuple[Any, ...]
    missing: tuple[bool, ...]
    doc_ord: int
    score: float
    sort: tuple[SortSpec, ...]

    def __lt__(self, other: _RankEntry) -> bool:
        return _rank_compare(self, other) < 0


def _rank_compare(a: _RankEntry, b: _RankEntry) -> int:
    """Compare two candidates field by field: positive means ``a`` ranks first.

    A document missing a sort field always ranks after one that has it,
    regardless of that field's direction — the same rule Elasticsearch/Lucene
    use. Ties fall through to the next sort key; a full tie on every key
    breaks on ascending ``doc_ord`` — arbitrary, but deterministic, matching
    this project's usual tie-break style (see
    :func:`scopiengine.index.searcher.search`).
    """
    for v1, m1, v2, m2, spec in zip(
        a.keyvals, a.missing, b.keyvals, b.missing, a.sort, strict=True
    ):
        if m1 and m2:
            continue
        if m1 != m2:
            return -1 if m1 else 1
        if v1 == v2:
            continue
        if spec.descending:
            return 1 if v1 > v2 else -1
        return 1 if v1 < v2 else -1
    if a.doc_ord == b.doc_ord:
        return 0
    return 1 if a.doc_ord < b.doc_ord else -1


def _stream_sorted_hits(
    engine: Engine,
    index: str,
    mapping: Mapping,
    query: Query,
    sort: tuple[SortSpec, ...],
    *,
    from_: int,
    size: int,
    max_sort_candidates: int,
    batch_size: int = 500,
) -> tuple[list[Hit], int, bool]:
    """The true index-wide top ``from_ + size`` by ``sort`` — never a re-sort
    of a relevance-ranked page. See the module docstring for the full design.

    Returns:
        ``(hits, total, truncated)`` — ``hits`` is already the final
        ``from_:from_ + size`` page, ordered by ``sort``; ``total`` is the
        true, complete match count regardless of truncation; ``truncated``
        is whether ``max_sort_candidates`` was hit before every match was
        examined.
    """
    limit = from_ + size
    reader = IndexReader(engine.storage, index, mapping)
    heap: list[_RankEntry] = []
    total = 0
    examined = 0
    truncated = False

    def flush(pending: list[Hit]) -> None:
        if not pending:
            return
        ords = [hit.doc_ord for hit in pending]
        stored = {doc.doc_ord: doc for doc in engine.storage.get_documents(index, ords)}
        for hit in pending:
            stored_doc = stored.get(hit.doc_ord)
            doc = json.loads(stored_doc.source) if stored_doc is not None else {}
            keyvals: list[Any] = []
            missing: list[bool] = []
            for spec in sort:
                value = hit.score if spec.field == "_score" else _extract_field(doc, spec.field)
                keyvals.append(value)
                missing.append(value is None)
            entry = _RankEntry(
                keyvals=tuple(keyvals),
                missing=tuple(missing),
                doc_ord=hit.doc_ord,
                score=hit.score,
                sort=sort,
            )
            if limit <= 0:
                continue
            if len(heap) < limit:
                heapq.heappush(heap, entry)
            elif heap[0] < entry:
                heapq.heapreplace(heap, entry)

    batch: list[Hit] = []
    for hit in iter_matches(reader, query):
        total += 1
        if examined < max_sort_candidates:
            batch.append(hit)
            examined += 1
            if len(batch) >= batch_size:
                flush(batch)
                batch = []
        else:
            # Past the cap: keep counting for an accurate `total`, but stop
            # fetching source and stop trying to rank — the module docstring
            # and `Settings.max_sort_candidates` cover what this means for
            # the caller.
            truncated = True
    flush(batch)

    ranked = sorted(heap, reverse=True)
    page = ranked[from_ : from_ + size]
    hits = [Hit(doc_ord=entry.doc_ord, score=entry.score) for entry in page]
    return hits, total, truncated


def terms_aggregation(
    engine: Engine,
    index: str,
    mapping: Mapping,
    query: Query,
    agg: TermsAgg,
    *,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Run one ``terms`` aggregation: bucket every match by ``agg.field``'s value, counted.

    Streams every match (:func:`scopiengine.index.searcher.iter_matches`,
    never a ranked top-N page) and fetches stored source in bounded batches —
    memory scales with the number of *distinct* bucket keys plus one batch,
    never with the number of matches.

    Returns:
        An Elasticsearch-shaped terms aggregation result: ``buckets`` (top
        ``agg.size`` by count, ties broken by key), ``sum_other_doc_count``
        for everything past that cut, and ``doc_count_error_upper_bound``
        (always ``0`` — every match is actually counted, not sampled).
    """
    reader = IndexReader(engine.storage, index, mapping)
    counts: dict[str, int] = {}

    def flush(batch: list[int]) -> None:
        if not batch:
            return
        for stored in engine.storage.get_documents(index, batch):
            value = _extract_field(json.loads(stored.source), agg.field)
            if value is None:
                continue
            key = str(value)
            counts[key] = counts.get(key, 0) + 1

    batch: list[int] = []
    for hit in iter_matches(reader, query):
        batch.append(hit.doc_ord)
        if len(batch) >= batch_size:
            flush(batch)
            batch = []
    flush(batch)

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    buckets = ranked[: agg.size]
    other = sum(count for _key, count in ranked[agg.size :])
    return {
        "doc_count_error_upper_bound": 0,
        "sum_other_doc_count": other,
        "buckets": [{"key": key, "doc_count": count} for key, count in buckets],
    }


def _render(
    engine: Engine,
    index: str,
    query: Query,
    *,
    size: int,
    from_: int,
    sort: tuple[SortSpec, ...],
    source: bool | tuple[str, ...],
    aggs: dict[str, TermsAgg],
    mapping: Mapping,
) -> dict[str, Any]:
    started = time.perf_counter()

    sort_truncated: bool | None = None
    if _needs_index_wide_sort(sort):
        hits, total, sort_truncated = _stream_sorted_hits(
            engine,
            index,
            mapping,
            query,
            sort,
            from_=from_,
            size=size,
            max_sort_candidates=engine.settings.max_sort_candidates,
        )
        if sort_truncated:
            _logger.warning(
                "field sort on index %r truncated after examining "
                "max_sort_candidates=%d matches (true total=%d); results are "
                "drawn only from the examined candidates and may not be the "
                "exact index-wide top %d — raise Settings.max_sort_candidates "
                "if this index's field sorts need a wider guarantee",
                index,
                engine.settings.max_sort_candidates,
                total,
                from_ + size,
            )
    else:
        result = engine.search(index, query, size=size, from_=from_)
        hits, total = list(result.hits), result.total

    ords = [hit.doc_ord for hit in hits]
    stored = engine.storage.get_documents(index, ords) if ords else []
    by_ord = {doc.doc_ord: doc for doc in stored}

    hits_json: list[dict[str, Any]] = []
    for hit in hits:
        stored_doc = by_ord.get(hit.doc_ord)
        if stored_doc is None:
            continue
        entry: dict[str, Any] = {"_index": index, "_id": stored_doc.doc_id, "_score": hit.score}
        projected = _project_source(json.loads(stored_doc.source), source)
        if projected is not None:
            entry["_source"] = projected
        hits_json.append(entry)

    aggregations = (
        {
            name: terms_aggregation(engine, index, mapping, query, spec)
            for name, spec in aggs.items()
        }
        if aggs
        else None
    )

    took_ms = (time.perf_counter() - started) * 1000
    segments_touched = int(engine.stats(index).get("segment_count", 0))
    max_score = max((hit.score for hit in hits), default=None)

    scopi_block: dict[str, Any] = {
        "query": ast_to_dict(query),
        "segments_touched": segments_touched,
        "took_ms": round(took_ms, 3),
    }
    if sort_truncated is not None:
        scopi_block["sort_truncated"] = sort_truncated
        if sort_truncated:
            scopi_block["max_sort_candidates"] = engine.settings.max_sort_candidates

    response: dict[str, Any] = {
        "took": round(took_ms),
        "timed_out": False,
        "hits": {
            "total": {"value": total, "relation": "eq"},
            "max_score": max_score,
            "hits": hits_json,
        },
        "scopi": scopi_block,
    }
    if aggregations is not None:
        response["aggregations"] = aggregations
    return response


def run_scopiql(
    engine: Engine,
    index: str,
    text: str,
    *,
    size: int = 10,
    from_: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse and execute a ScopiQL query against ``index``, rendering the envelope.

    ``size``/``from_`` are the request's defaults; a ``| limit N`` pipeline
    stage overrides ``size`` (ScopiQL 1.0 has no offset stage, so ``from_``
    always comes from the caller).

    Raises:
        IndexNotFoundError: No index named ``index`` exists.
        InvalidQueryError: ``text`` is not valid ScopiQL.
        UnsupportedFeatureError: ``text`` uses a feature this build does not implement.
    """
    mapping = engine.get_mapping(index)
    parsed = parse_scopiql(text, mapping, engine.analyzers, now=now)
    effective_size = parsed.limit if parsed.limit is not None else size
    source: bool | tuple[str, ...] = parsed.fields if parsed.fields is not None else True
    aggs = {"stats": TermsAgg(field=parsed.stats.by)} if parsed.stats is not None else {}

    envelope = _render(
        engine,
        index,
        parsed.query,
        size=effective_size,
        from_=from_,
        sort=parsed.sort,
        source=source,
        aggs=aggs,
        mapping=mapping,
    )
    envelope["scopi"]["scopiql"] = {
        "sort": [{"field": s.field, "descending": s.descending} for s in parsed.sort],
        "limit": parsed.limit,
        "fields": list(parsed.fields) if parsed.fields is not None else None,
        "stats": {"by": parsed.stats.by} if parsed.stats is not None else None,
    }
    return envelope


def run_dsl(engine: Engine, index: str, body: dict[str, Any]) -> dict[str, Any]:
    """Compile and execute a JSON DSL request body against ``index``, rendering the envelope.

    Raises:
        IndexNotFoundError: No index named ``index`` exists.
        InvalidQueryError: ``body`` is malformed.
        UnsupportedFeatureError: ``body`` names a clause or option this build
            does not implement — the error names the exact key.
    """
    mapping = engine.get_mapping(index)
    parsed = compile_dsl(mapping, engine.analyzers, body)
    return _render(
        engine,
        index,
        parsed.query,
        size=parsed.size,
        from_=parsed.from_,
        sort=parsed.sort,
        source=parsed.source,
        aggs=parsed.aggs,
        mapping=mapping,
    )
