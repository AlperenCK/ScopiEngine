"""Field sort is a genuine index-wide sort, not a re-sort of a relevance-ranked
page: the regression this guards against is a query where relevance and the
sort field disagree, which a page-local implementation gets silently wrong.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from scopiengine.engine import Engine
from scopiengine.query.results import run_dsl, run_scopiql
from scopiengine.settings import Settings

_MAPPING = {
    "properties": {
        "level": {"type": "keyword"},
        "service": {"type": "keyword"},
        "message": {"type": "text"},
        "n": {"type": "long"},
        "@timestamp": {"type": "date"},
    }
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = Settings(storage=f"sqlite:///{tmp_path / 'scopi.db'}")
    with Engine.open(settings) as eng:
        yield eng


def _hit_ids(envelope: dict) -> list[str]:
    return [h["_id"] for h in envelope["hits"]["hits"]]


# -- the regression: relevance and the sort field disagree -------------------


@pytest.fixture
def disagreeing_relevance_and_time(engine: Engine) -> Engine:
    """30 highly-relevant-but-old ERROR docs, plus 3 barely-relevant-but-newest ones.

    A page-local "rank by relevance, then reorder that page" sort would
    return three of the old, highly-relevant documents — never the three
    genuinely newest ones, since none of the `new-*` docs is anywhere near
    the top of the relevance ranking. Large enough (33 matches, `limit 3`)
    that the page-local bug is not a coincidental pass.
    """
    engine.create_index("logs", _MAPPING)
    docs = []
    ids = []
    for i in range(30):
        docs.append(
            {
                "level": "ERROR",
                "message": "error error error error connection connection",
                "@timestamp": f"2020-01-{i % 28 + 1:02d}T00:00:00Z",
            }
        )
        ids.append(f"old-{i}")
    for i in range(3):
        docs.append(
            {"level": "ERROR", "message": "boom", "@timestamp": f"2026-08-{16 - i:02d}T00:00:00Z"}
        )
        ids.append(f"new-{i}")
    engine.index_documents("logs", docs, ids=ids)
    engine.refresh("logs")
    return engine


def test_sort_returns_the_true_newest_not_the_most_relevant(
    disagreeing_relevance_and_time: Engine,
) -> None:
    envelope = run_scopiql(
        disagreeing_relevance_and_time, "logs", "level:ERROR | sort -@timestamp | limit 3"
    )
    assert _hit_ids(envelope) == ["new-0", "new-1", "new-2"]
    assert envelope["hits"]["total"]["value"] == 33
    assert envelope["scopi"].get("sort_truncated") is False


def test_sort_parity_between_scopiql_and_dsl_on_the_disagreeing_case(
    disagreeing_relevance_and_time: Engine,
) -> None:
    via_scopiql = run_scopiql(
        disagreeing_relevance_and_time, "logs", "level:ERROR | sort -@timestamp | limit 3"
    )
    via_dsl = run_dsl(
        disagreeing_relevance_and_time,
        "logs",
        {"query": {"term": {"level": "ERROR"}}, "sort": [{"@timestamp": "desc"}], "size": 3},
    )
    assert _hit_ids(via_scopiql) == _hit_ids(via_dsl) == ["new-0", "new-1", "new-2"]


# -- multi-key sort, ascending vs descending ----------------------------------


@pytest.fixture
def multi_key_docs(engine: Engine) -> Engine:
    engine.create_index("logs", _MAPPING)
    # Two services; within each, `n` is what should break the tie.
    docs = [
        {"service": "auth", "n": 3, "level": "INFO"},
        {"service": "auth", "n": 1, "level": "INFO"},
        {"service": "billing", "n": 2, "level": "INFO"},
        {"service": "auth", "n": 2, "level": "INFO"},
        {"service": "billing", "n": 1, "level": "INFO"},
    ]
    ids = ["a3", "a1", "b2", "a2", "b1"]
    engine.index_documents("logs", docs, ids=ids)
    engine.refresh("logs")
    return engine


def test_multi_key_sort_service_asc_then_n_desc(multi_key_docs: Engine) -> None:
    envelope = run_scopiql(multi_key_docs, "logs", "level:INFO | sort service,-n | limit 10")
    assert _hit_ids(envelope) == ["a3", "a2", "a1", "b2", "b1"]


def test_single_field_ascending(multi_key_docs: Engine) -> None:
    envelope = run_scopiql(multi_key_docs, "logs", "service:auth | sort n | limit 10")
    assert _hit_ids(envelope) == ["a1", "a2", "a3"]


def test_single_field_descending(multi_key_docs: Engine) -> None:
    envelope = run_scopiql(multi_key_docs, "logs", "service:auth | sort -n | limit 10")
    assert _hit_ids(envelope) == ["a3", "a2", "a1"]


def test_dsl_sort_object_form_ascending(multi_key_docs: Engine) -> None:
    envelope = run_dsl(
        multi_key_docs, "logs", {"query": {"term": {"service": "auth"}}, "sort": ["n"]}
    )
    assert _hit_ids(envelope) == ["a1", "a2", "a3"]


# -- documents missing the sort field keep a stable, direction-independent placement --


def test_documents_missing_the_sort_field_sort_last_ascending(engine: Engine) -> None:
    engine.create_index("logs", _MAPPING)
    engine.index_documents(
        "logs",
        [
            {"level": "INFO", "n": 5},
            {"level": "INFO"},  # no `n`
            {"level": "INFO", "n": 1},
        ],
        ids=["has-5", "missing", "has-1"],
    )
    engine.refresh("logs")
    envelope = run_scopiql(engine, "logs", "level:INFO | sort n | limit 10")
    assert _hit_ids(envelope) == ["has-1", "has-5", "missing"]


def test_documents_missing_the_sort_field_sort_last_descending(engine: Engine) -> None:
    engine.create_index("logs", _MAPPING)
    engine.index_documents(
        "logs",
        [
            {"level": "INFO", "n": 5},
            {"level": "INFO"},  # no `n`
            {"level": "INFO", "n": 1},
        ],
        ids=["has-5", "missing", "has-1"],
    )
    engine.refresh("logs")
    envelope = run_scopiql(engine, "logs", "level:INFO | sort -n | limit 10")
    assert _hit_ids(envelope) == ["has-5", "has-1", "missing"]


# -- truncation: the flag, the true total, and results still from what was seen --


def test_truncation_flag_true_total_and_results_from_examined_set(tmp_path: Path) -> None:
    settings = Settings(storage=f"sqlite:///{tmp_path / 'truncated.db'}", max_sort_candidates=5)
    with Engine.open(settings) as eng:
        eng.create_index("logs", _MAPPING)
        docs = [{"level": "ERROR", "n": i} for i in range(20)]
        ids = [str(i) for i in range(20)]
        eng.index_documents("logs", docs, ids=ids)
        eng.refresh("logs")

        envelope = run_scopiql(eng, "logs", "level:ERROR | sort -n | limit 3")

        assert envelope["scopi"]["sort_truncated"] is True
        assert envelope["scopi"]["max_sort_candidates"] == 5
        # `total` is the true, complete match count, not capped by the candidate scan.
        assert envelope["hits"]["total"]["value"] == 20
        # The page is drawn only from the first `max_sort_candidates` matches
        # examined (ordinals 0..4, since a single-term match streams in
        # ascending doc-ordinal order) — never from documents beyond the cap.
        returned_ids = {h["_id"] for h in envelope["hits"]["hits"]}
        assert returned_ids <= {"0", "1", "2", "3", "4"}


def test_no_truncation_flag_when_under_the_cap(tmp_path: Path) -> None:
    settings = Settings(storage=f"sqlite:///{tmp_path / 'untrunc.db'}", max_sort_candidates=5)
    with Engine.open(settings) as eng:
        eng.create_index("logs", _MAPPING)
        eng.index_documents(
            "logs", [{"level": "ERROR", "n": i} for i in range(3)], ids=["0", "1", "2"]
        )
        eng.refresh("logs")
        envelope = run_scopiql(eng, "logs", "level:ERROR | sort -n | limit 10")
        assert envelope["scopi"]["sort_truncated"] is False
        assert "max_sort_candidates" not in envelope["scopi"]
        assert envelope["hits"]["total"]["value"] == 3


def test_pure_score_sort_never_sets_sort_truncated(engine: Engine) -> None:
    engine.create_index("logs", _MAPPING)
    engine.index_documents("logs", [{"level": "ERROR", "message": "boom"}], ids=["1"])
    engine.refresh("logs")
    envelope = run_scopiql(engine, "logs", "message:boom")
    assert "sort_truncated" not in envelope["scopi"]


def test_max_sort_candidates_setting_validates_positive() -> None:
    from scopiengine.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        Settings(max_sort_candidates=0)
