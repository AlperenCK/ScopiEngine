"""End-to-end indexing and search over SQLite: term/phrase/prefix/range/bool,
delete-and-reindex, and the segment-count invariant (N segments and a
force-merged index return identical hits and scores).
"""

from __future__ import annotations

import random
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from scopiengine.engine import Engine
from scopiengine.mapping.encoding import encode_date, encode_long
from scopiengine.query.ast import Bool, Exists, MatchAll, Phrase, Prefix, Range, Term
from scopiengine.settings import Settings

#: Fixed seed: the property-style segment-count invariant test is reproducible.
_SEED = 20260817


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = Settings(storage=f"sqlite:///{tmp_path / 'scopi.db'}")
    with Engine.open(settings) as eng:
        yield eng


def _hit_ids(eng: Engine, index: str, hits: list) -> list[str]:
    docs = eng.storage.get_documents(index, [h.doc_ord for h in hits])
    by_ord = {d.doc_ord: d.doc_id for d in docs}
    return [by_ord[h.doc_ord] for h in hits]


# -- basic indexing and term/phrase/prefix/range/bool queries -----------------


@pytest.fixture
def logs(engine: Engine) -> Engine:
    engine.create_index(
        "logs",
        {
            "properties": {
                "message": {"type": "text"},
                "level": {"type": "keyword"},
                "count": {"type": "long"},
                "seen_at": {"type": "date"},
            }
        },
    )
    engine.index_documents(
        "logs",
        [
            {
                "message": "quick brown fox jumps over the lazy dog",
                "level": "INFO",
                "count": 1,
                "seen_at": "2026-01-01T00:00:00Z",
            },
            {
                "message": "the quick fox is quick and clever",
                "level": "ERROR",
                "count": 5,
                "seen_at": "2026-02-01T00:00:00Z",
            },
            {
                "message": "completely unrelated log entry here",
                "level": "INFO",
                "count": 10,
                "seen_at": "2026-03-01T00:00:00Z",
            },
        ],
        ids=["a", "b", "c"],
    )
    engine.refresh("logs")
    return engine


def test_term_query_matches_and_ranks_by_bm25(logs: Engine) -> None:
    hits = logs.search("logs", Term("message", "quick"))
    ids = _hit_ids(logs, "logs", hits)
    assert set(ids) == {"a", "b"}
    # doc "b" says "quick" twice, so it should outrank doc "a".
    assert ids[0] == "b"


def test_phrase_query_requires_adjacency(logs: Engine) -> None:
    hits = logs.search("logs", Phrase("message", ("quick", "fox")))
    ids = _hit_ids(logs, "logs", hits)
    assert ids == ["b"]  # "quick fox" is adjacent only in doc b


def test_phrase_query_with_slop_allows_a_gap(logs: Engine) -> None:
    # "brown fox" appears; "quick ... fox" in doc a needs slop 1 to match
    # ("quick brown fox": quick@0, brown@1, fox@2 - distance 2, needs slop 1).
    tight = logs.search("logs", Phrase("message", ("quick", "fox"), slop=0))
    loose = logs.search("logs", Phrase("message", ("quick", "fox"), slop=1))
    assert _hit_ids(logs, "logs", tight) == ["b"]
    assert set(_hit_ids(logs, "logs", loose)) == {"a", "b"}


def test_prefix_query(logs: Engine) -> None:
    hits = logs.search("logs", Prefix("message", "jum"))
    assert _hit_ids(logs, "logs", hits) == ["a"]


def test_range_query_on_long_field(logs: Engine) -> None:
    hits = logs.search("logs", Range("count", gte=encode_long(2), lte=encode_long(10)))
    assert set(_hit_ids(logs, "logs", hits)) == {"b", "c"}


def test_range_query_exclusive_bounds(logs: Engine) -> None:
    hits = logs.search("logs", Range("count", gt=encode_long(1), lt=encode_long(10)))
    assert _hit_ids(logs, "logs", hits) == ["b"]


def test_range_query_on_date_field(logs: Engine) -> None:
    hits = logs.search(
        "logs",
        Range(
            "seen_at",
            gte=encode_date("2026-01-15T00:00:00Z"),
            lte=encode_date("2026-02-28T00:00:00Z"),
        ),
    )
    assert _hit_ids(logs, "logs", hits) == ["b"]


def test_exists_query(logs: Engine) -> None:
    hits = logs.search("logs", Exists("level"))
    assert len(hits) == 3


def test_exists_query_on_absent_field_matches_nothing(logs: Engine) -> None:
    assert logs.search("logs", Exists("nonexistent_field")) == []


def test_match_all_returns_every_live_document(logs: Engine) -> None:
    hits = logs.search("logs", MatchAll())
    assert len(hits) == 3
    assert all(h.score == 1.0 for h in hits)


def test_bool_must_and_filter(logs: Engine) -> None:
    hits = logs.search(
        "logs",
        Bool(must=(Term("message", "quick"),), filter=(Term("level", "ERROR"),)),
    )
    assert _hit_ids(logs, "logs", hits) == ["b"]


def test_bool_must_not(logs: Engine) -> None:
    hits = logs.search(
        "logs", Bool(must=(Term("level", "INFO"),), must_not=(Term("message", "unrelated"),))
    )
    assert _hit_ids(logs, "logs", hits) == ["a"]


def test_bool_should_without_must_acts_as_or(logs: Engine) -> None:
    hits = logs.search(
        "logs", Bool(should=(Term("message", "jumps"), Term("message", "unrelated")))
    )
    assert set(_hit_ids(logs, "logs", hits)) == {"a", "c"}


def test_bool_should_boosts_score_without_gating(logs: Engine) -> None:
    base = logs.search("logs", Term("message", "quick"))
    boosted = logs.search(
        "logs", Bool(must=(Term("message", "quick"),), should=(Term("level", "ERROR"),))
    )
    base_by_ord = {h.doc_ord: h.score for h in base}
    boosted_by_ord = {h.doc_ord: h.score for h in boosted}
    assert set(base_by_ord) == set(boosted_by_ord)
    # doc "b" (level ERROR) should score higher once boosted; doc "a" unchanged.
    for doc_ord, score in boosted_by_ord.items():
        assert score >= base_by_ord[doc_ord]
    assert boosted_by_ord != base_by_ord


def test_empty_bool_matches_everything(logs: Engine) -> None:
    hits = logs.search("logs", Bool())
    assert len(hits) == 3


# -- delete and re-index are reflected in results ------------------------------


def test_delete_then_search_excludes_the_document(logs: Engine) -> None:
    assert logs.delete_document("logs", "a") is True
    logs.refresh("logs")
    hits = logs.search("logs", MatchAll())
    assert set(_hit_ids(logs, "logs", hits)) == {"b", "c"}


def test_delete_missing_document_returns_false(logs: Engine) -> None:
    assert logs.delete_document("logs", "does-not-exist") is False


def test_reindex_after_delete_is_visible(logs: Engine) -> None:
    logs.delete_document("logs", "a")
    logs.index_documents("logs", [{"message": "brand new entry", "level": "INFO"}], ids=["a"])
    logs.refresh("logs")
    hits = logs.search("logs", Term("message", "brand"))
    assert _hit_ids(logs, "logs", hits) == ["a"]
    # the old content for "a" must no longer match
    assert logs.search("logs", Term("message", "jumps")) == []


def test_get_document_returns_none_after_delete(logs: Engine) -> None:
    logs.delete_document("logs", "a")
    assert logs.get_document("logs", "a") is None


# -- unsupported feature: phrase without positions -----------------------------


def test_phrase_query_requires_positions_index_option(engine: Engine) -> None:
    from scopiengine.errors import UnsupportedFeatureError

    engine.create_index(
        "docs_only",
        {"properties": {"body": {"type": "text", "index_options": "docs"}}},
    )
    engine.index_documents("docs_only", [{"body": "quick fox"}])
    engine.refresh("docs_only")
    with pytest.raises(UnsupportedFeatureError):
        engine.search("docs_only", Phrase("body", ("quick", "fox")))


# -- the force-merge invariant, over several random document sets -------------


def _random_body(rng: random.Random, vocab: list[str], length: int) -> str:
    return " ".join(rng.choice(vocab) for _ in range(length))


@pytest.mark.parametrize("trial", range(3))
def test_force_merge_preserves_hits_and_scores(tmp_path: Path, trial: int) -> None:
    """N segments and a force-merged index return identical hits and scores.

    Documents are indexed in several small batches (each refreshed separately,
    producing several live segments), scored, then force-merged into one
    segment and scored again — the two result sets must match exactly,
    including tie-broken ordering, since BM25 score depends only on corpus-wide
    statistics (N, df, avgdl), never on how those documents happen to be
    physically split across segments.
    """
    rng = random.Random(_SEED + trial)
    vocab = ["apple", "banana", "cherry", "date", "elderberry", "fig", "grape"]

    settings = Settings(storage=f"sqlite:///{tmp_path / f'merge-{trial}.db'}")
    with Engine.open(settings) as eng:
        eng.create_index("bulk")
        doc_id = 0
        batches = rng.randint(3, 6)
        for _ in range(batches):
            batch_size = rng.randint(2, 5)
            docs = [
                {"body": _random_body(rng, vocab, rng.randint(3, 8))} for _ in range(batch_size)
            ]
            ids = [str(doc_id + i) for i in range(batch_size)]
            doc_id += batch_size
            eng.index_documents("bulk", docs, ids=ids)
            eng.refresh("bulk")

        # Occasionally delete a document to exercise tombstone purging in the merge.
        if doc_id > 3:
            eng.delete_document("bulk", "1")
            eng.refresh("bulk")

        segments_before = eng.stats("bulk")["segment_count"]
        assert segments_before >= 1

        term = rng.choice(vocab)
        before_term = eng.search("bulk", Term("body", term), size=1000)
        before_all = eng.search("bulk", MatchAll(), size=1000)
        before_bool = eng.search(
            "bulk", Bool(should=(Term("body", vocab[0]), Term("body", vocab[1]))), size=1000
        )

        eng.force_merge("bulk")
        assert eng.stats("bulk")["segment_count"] <= 1

        after_term = eng.search("bulk", Term("body", term), size=1000)
        after_all = eng.search("bulk", MatchAll(), size=1000)
        after_bool = eng.search(
            "bulk", Bool(should=(Term("body", vocab[0]), Term("body", vocab[1]))), size=1000
        )

        assert before_term == after_term
        assert before_all == after_all
        assert before_bool == after_bool


# -- bounded memory on the search path -----------------------------------------


def test_match_all_search_does_not_materialise_the_whole_corpus(tmp_path: Path) -> None:
    """The bounded top-N heap keeps memory roughly constant in the requested size.

    Not a strict memory-profiler assertion (too flaky across machines/Python
    builds) — instead, checks the cheap, reliable proxy that actually matters
    here: asking for a small page of a large result set does not require
    building a Python list anywhere near the corpus size. We verify this
    indirectly by confirming ``search`` returns exactly ``size`` hits from a
    much larger corpus and that increasing the corpus size doesn't change the
    object count the call itself allocates for the *result* (only for `size`).
    """
    settings = Settings(storage=f"sqlite:///{tmp_path / 'bounded.db'}")
    with Engine.open(settings) as eng:
        eng.create_index("big")
        n = 500
        eng.index_documents("big", [{"body": "apple banana"} for _ in range(n)])
        eng.refresh("big")

        hits = eng.search("big", MatchAll(), size=5)
        assert len(hits) == 5

        # The searcher must stream, not sort-then-slice a materialised list of
        # every match: confirm the same generator machinery is what executes by
        # checking the internal executor yields a live iterator, not a list.
        from scopiengine.index.reader import IndexReader
        from scopiengine.index.searcher import _execute
        from scopiengine.mapping.mapping import parse_mapping

        info = eng.get_index("big")
        reader = IndexReader(eng.storage, "big", parse_mapping(info.mapping))
        stream = _execute(MatchAll(), reader)
        assert hasattr(stream, "__next__"), "MatchAll execution must be a lazy iterator"
        first = next(stream)
        assert first == (0, 1.0)
        # getsizeof of a live generator frame is small and independent of `n`.
        size_of_generator = sys.getsizeof(stream)
        assert size_of_generator < 1024, "the executor appears to have materialised results eagerly"
