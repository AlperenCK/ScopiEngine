"""The REST API, end to end: create an index, bulk-load it, search it both ways
(ScopiQL and JSON DSL), fetch/stat/delete — plus the ScopiQL/DSL parity table
that is the whole guarantee behind having two query languages compile to one AST.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopiengine.api import create_app
from scopiengine.settings import Settings

_MAPPING = {
    "properties": {
        "level": {"type": "keyword"},
        "service": {"type": "keyword"},
        "status": {"type": "long"},
        "message": {"type": "text"},
        "trace_id": {"type": "keyword"},
    }
}

_DOCS: list[tuple[str, dict]] = [
    ("1", {"level": "ERROR", "service": "auth", "status": 500, "message": "connection refused"}),
    ("2", {"level": "INFO", "service": "auth", "status": 200, "message": "all good here"}),
    (
        "3",
        {
            "level": "ERROR",
            "service": "billing",
            "status": 502,
            "message": "payment failed badly",
            "trace_id": "abc123",
        },
    ),
    ("4", {"level": "WARN", "service": "billing", "status": 429, "message": "rate limited"}),
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(storage=f"sqlite:///{tmp_path / 'api.db'}")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _bulk_body(docs: list[tuple[str, dict]]) -> str:
    lines = []
    for doc_id, source in docs:
        lines.append(json.dumps({"index": {"_id": doc_id}}))
        lines.append(json.dumps(source))
    return "\n".join(lines) + "\n"


@pytest.fixture
def logs(client: TestClient) -> TestClient:
    r = client.put("/logs", json={"mappings": _MAPPING})
    assert r.status_code == 201
    r = client.post(
        "/logs/_bulk",
        content=_bulk_body(_DOCS),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert r.status_code == 200
    assert r.json()["errors"] is False
    r = client.post("/logs/_refresh")
    assert r.status_code == 200
    return client


# -- full lifecycle ----------------------------------------------------------


def test_full_lifecycle(client: TestClient) -> None:
    created = client.put("/lifecycle", json={"mappings": _MAPPING})
    assert created.status_code == 201
    assert created.json() == {"acknowledged": True, "index": "lifecycle"}

    bulk = client.post(
        "/lifecycle/_bulk",
        content=_bulk_body(_DOCS),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert bulk.status_code == 200
    body = bulk.json()
    assert body["errors"] is False
    assert len(body["items"]) == len(_DOCS)
    assert all(item["index"]["status"] == 201 for item in body["items"])

    refreshed = client.post("/lifecycle/_refresh")
    assert refreshed.status_code == 200

    scopiql_search = client.get("/lifecycle/_search", params={"q": "level:ERROR"})
    assert scopiql_search.status_code == 200
    assert scopiql_search.json()["hits"]["total"]["value"] == 2

    dsl_search = client.post("/lifecycle/_search", json={"query": {"term": {"level": "ERROR"}}})
    assert dsl_search.status_code == 200
    assert dsl_search.json()["hits"]["total"]["value"] == 2

    doc = client.get("/lifecycle/_doc/1")
    assert doc.status_code == 200
    assert doc.json()["_source"]["service"] == "auth"

    stats = client.get("/lifecycle/_stats")
    assert stats.status_code == 200
    assert stats.json()["live_doc_count"] == len(_DOCS)

    deleted = client.delete("/lifecycle")
    assert deleted.status_code == 200
    assert deleted.json() == {"acknowledged": True}
    assert client.head("/lifecycle").status_code == 404


def test_head_index_exists(logs: TestClient) -> None:
    assert logs.head("/logs").status_code == 200
    assert logs.head("/does-not-exist").status_code == 404


def test_get_index_shows_mapping(logs: TestClient) -> None:
    r = logs.get("/logs")
    assert r.status_code == 200
    assert "level" in r.json()["logs"]["mappings"]["properties"]


def test_index_already_exists_is_409(logs: TestClient) -> None:
    r = logs.put("/logs", json={"mappings": _MAPPING})
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "index_already_exists"


def test_index_not_found_is_404(client: TestClient) -> None:
    r = client.get("/does-not-exist/_stats")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "index_not_found"


def test_doc_put_get_delete(logs: TestClient) -> None:
    put = logs.put("/logs/_doc/put-1", json={"level": "INFO", "message": "hello"})
    assert put.status_code == 201
    got = logs.get("/logs/_doc/put-1")
    assert got.status_code == 200
    assert got.json()["_source"]["message"] == "hello"
    deleted = logs.delete("/logs/_doc/put-1")
    assert deleted.status_code == 200
    missing = logs.get("/logs/_doc/put-1")
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "document_not_found"


def test_doc_post_auto_id(logs: TestClient) -> None:
    r = logs.post("/logs/_doc", json={"level": "INFO", "message": "auto id"})
    assert r.status_code == 201
    doc_id = r.json()["_id"]
    assert logs.get(f"/logs/_doc/{doc_id}").status_code == 200


def test_merge_endpoint(logs: TestClient) -> None:
    r = logs.post("/logs/_forcemerge")
    assert r.status_code == 200


# -- bulk: per-item results and the errors flag -------------------------------


def test_bulk_per_item_results_and_errors_flag(logs: TestClient) -> None:
    body = (
        "\n".join(
            [
                json.dumps({"index": {"_id": "new-1"}}),
                json.dumps({"level": "INFO", "message": "fresh"}),
                json.dumps({"delete": {"_id": "does-not-exist"}}),
            ]
        )
        + "\n"
    )
    r = logs.post("/logs/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["errors"] is True
    assert payload["items"][0]["index"]["status"] == 201
    assert payload["items"][0]["index"]["_id"] == "new-1"
    assert payload["items"][1]["delete"]["status"] == 404
    assert payload["items"][1]["delete"]["result"] == "not_found"


def test_bulk_unsupported_action_is_400(logs: TestClient) -> None:
    body = json.dumps({"update": {"_id": "1"}}) + "\n" + json.dumps({"doc": {}}) + "\n"
    r = logs.post("/logs/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 400
    assert "update" in r.json()["error"]["reason"]


def test_top_level_bulk_requires_index_per_action(client: TestClient) -> None:
    client.put("/a", json={"mappings": _MAPPING})
    body = (
        json.dumps({"index": {"_index": "a", "_id": "1"}})
        + "\n"
        + json.dumps({"level": "INFO"})
        + "\n"
    )
    r = client.post("/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 200
    assert r.json()["items"][0]["index"]["_index"] == "a"


# -- unsupported DSL key: 400, naming the key ---------------------------------


def test_unsupported_dsl_key_returns_400_naming_the_key(logs: TestClient) -> None:
    r = logs.post("/logs/_search", json={"query": {"fuzzy": {"message": "conn"}}})
    assert r.status_code == 400
    payload = r.json()
    assert payload["error"]["type"] == "unsupported_feature"
    assert "fuzzy" in payload["error"]["reason"]


def test_unsupported_top_level_option_returns_400_naming_the_key(logs: TestClient) -> None:
    r = logs.post("/logs/_search", json={"query": {"match_all": {}}, "min_score": 1})
    assert r.status_code == 400
    assert "min_score" in r.json()["error"]["reason"]


# -- _cat/indices and _plugins -------------------------------------------------


def test_cat_indices(logs: TestClient) -> None:
    r = logs.get("/_cat/indices")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["index"] == "logs" and row["docs_count"] == len(_DOCS) for row in rows)


def test_plugins_endpoint(logs: TestClient) -> None:
    r = logs.get("/_plugins")
    assert r.status_code == 200
    payload = r.json()
    assert "scopiengine.plugins.builtin.analyzers" in payload["loaded"]
    assert payload["failed"] == []


def test_root_and_health(client: TestClient) -> None:
    assert client.get("/").json()["name"] == "scopiengine"
    assert client.get("/_health").json() == {"status": "ok"}
    health = client.get("/_cluster/health")
    assert health.status_code == 200
    assert health.json()["status"] == "green"


def test_analyze_endpoint(logs: TestClient) -> None:
    r = logs.post("/logs/_analyze", json={"analyzer": "standard", "text": "Quick BROWN Fox"})
    assert r.status_code == 200
    tokens = [t["token"] for t in r.json()["tokens"]]
    assert tokens == ["quick", "brown", "fox"]


def test_scopiql_endpoint(logs: TestClient) -> None:
    r = logs.post("/logs/_scopiql", json={"query": "level:ERROR", "size": 1})
    assert r.status_code == 200
    assert r.json()["hits"]["total"]["value"] == 2
    assert len(r.json()["hits"]["hits"]) == 1


# -- total > size ------------------------------------------------------------


def test_total_reflects_true_match_count_beyond_page_size(client: TestClient) -> None:
    client.put("/big", json={"mappings": {"properties": {"body": {"type": "text"}}}})
    body = (
        "\n".join(
            json.dumps({"index": {"_id": str(i)}}) + "\n" + json.dumps({"body": "apple banana"})
            for i in range(50)
        )
        + "\n"
    )
    r = client.post("/big/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 200
    client.post("/big/_refresh")

    r = client.get("/big/_search", params={"q": "body:apple", "size": 5})
    payload = r.json()
    assert len(payload["hits"]["hits"]) == 5
    assert payload["hits"]["total"]["value"] == 50


# -- aggregations --------------------------------------------------------------


def test_aggs_terms(logs: TestClient) -> None:
    r = logs.post(
        "/logs/_search",
        json={
            "query": {"match_all": {}},
            "size": 0,
            "aggs": {"by_level": {"terms": {"field": "level"}}},
        },
    )
    assert r.status_code == 200
    buckets = {b["key"]: b["doc_count"] for b in r.json()["aggregations"]["by_level"]["buckets"]}
    assert buckets == {"ERROR": 2, "INFO": 1, "WARN": 1}


def test_scopiql_stats_stage_produces_aggregations(logs: TestClient) -> None:
    r = logs.get("/logs/_search", params={"q": "status:>=400 | stats count() by service"})
    assert r.status_code == 200
    payload = r.json()
    buckets = {b["key"]: b["doc_count"] for b in payload["aggregations"]["stats"]["buckets"]}
    assert buckets == {"auth": 1, "billing": 2}


# -- ScopiQL / DSL parity: identical hit sets and identical scores -----------

_PARITY_PAIRS: list[tuple[str, dict]] = [
    ("level:ERROR", {"query": {"term": {"level": "ERROR"}}}),
    (
        "level:ERROR AND service:auth",
        {
            "query": {
                "bool": {"must": [{"term": {"level": "ERROR"}}, {"term": {"service": "auth"}}]}
            }
        },
    ),
    ("status:>=500", {"query": {"range": {"status": {"gte": 500}}}}),
    (
        'message:"payment failed"',
        {"query": {"match_phrase": {"message": "payment failed"}}},
    ),
    ("message:conn*", {"query": {"prefix": {"message": "conn"}}}),
    (
        "level:(ERROR OR WARN)",
        {"query": {"terms": {"level": ["ERROR", "WARN"]}}},
    ),
    ("_exists_:trace_id", {"query": {"exists": {"field": "trace_id"}}}),
    (
        "status:>=400 AND NOT level:WARN",
        {
            "query": {
                "bool": {
                    "must": [{"range": {"status": {"gte": 400}}}],
                    "must_not": [{"term": {"level": "WARN"}}],
                }
            }
        },
    ),
]


@pytest.mark.parametrize("scopiql_text,dsl_body", _PARITY_PAIRS)
def test_scopiql_and_dsl_parity(logs: TestClient, scopiql_text: str, dsl_body: dict) -> None:
    """The same query, spelled two ways, returns identical hit sets and scores."""
    via_scopiql = logs.get("/logs/_search", params={"q": scopiql_text, "size": 50})
    via_dsl = logs.post("/logs/_search", json={**dsl_body, "size": 50})
    assert via_scopiql.status_code == 200, via_scopiql.text
    assert via_dsl.status_code == 200, via_dsl.text

    scopiql_hits = via_scopiql.json()["hits"]
    dsl_hits = via_dsl.json()["hits"]

    assert scopiql_hits["total"] == dsl_hits["total"]
    scopiql_pairs = [(h["_id"], h["_score"]) for h in scopiql_hits["hits"]]
    dsl_pairs = [(h["_id"], h["_score"]) for h in dsl_hits["hits"]]
    assert scopiql_pairs == dsl_pairs
