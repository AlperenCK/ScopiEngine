"""``POST /_ingest/file``, ``GET /_ingest/jobs``, ``GET`` and ``DELETE .../{id}``."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopiengine.api import create_app
from scopiengine.settings import Settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(storage=f"sqlite:///{tmp_path / 'api.db'}")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _write_json_log(path: Path, n: int) -> None:
    lines = [json.dumps({"seq": i, "level": "ERROR" if i % 5 == 0 else "INFO"}) for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wait_for_state(
    client: TestClient, job_id: str, states: set[str], *, timeout: float = 10.0
) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/_ingest/jobs/{job_id}")
        assert resp.status_code == 200
        payload = resp.json()
        if payload["state"] in states:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job never reached {states}, last saw {payload}")


def test_post_ingest_file_runs_to_completion(client: TestClient, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_json_log(log_path, 200)
    client.put("/logs")

    started = client.post(
        "/_ingest/file",
        json={"path": str(log_path), "index": "logs", "processors": ["json_line"]},
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    assert started.json()["state"] == "running"

    final = _wait_for_state(client, job_id, {"done"})
    assert final["docs_indexed"] == 200
    assert final["bytes_read"] == log_path.stat().st_size

    searched = client.get("/logs/_search", params={"q": "level:ERROR"})
    assert searched.status_code == 200
    assert searched.json()["hits"]["total"]["value"] == 40


def test_list_and_get_jobs(client: TestClient, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_json_log(log_path, 20)
    client.put("/logs")

    started = client.post("/_ingest/file", json={"path": str(log_path), "index": "logs"})
    job_id = started.json()["job_id"]
    _wait_for_state(client, job_id, {"done"})

    listed = client.get("/_ingest/jobs")
    assert listed.status_code == 200
    assert any(job["job_id"] == job_id for job in listed.json())

    fetched = client.get(f"/_ingest/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["job_id"] == job_id


def test_get_unknown_job_is_404(client: TestClient) -> None:
    resp = client.get("/_ingest/jobs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "ingest_job_not_found"


def test_delete_unknown_job_is_404(client: TestClient) -> None:
    resp = client.delete("/_ingest/jobs/does-not-exist")
    assert resp.status_code == 404


def test_delete_stops_a_running_job_cleanly(client: TestClient, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_json_log(log_path, 50_000)
    client.put("/logs")

    started = client.post(
        "/_ingest/file",
        json={
            "path": str(log_path),
            "index": "logs",
            "processors": ["json_line"],
            "batch_size": 20,
        },
    )
    job_id = started.json()["job_id"]

    # Give it a moment to make real progress before stopping it.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/_ingest/jobs/{job_id}").json()["bytes_read"] > 1000:
            break
        time.sleep(0.02)

    stopped = client.delete(f"/_ingest/jobs/{job_id}")
    assert stopped.status_code == 200

    final = _wait_for_state(client, job_id, {"stopped", "done"})
    assert 0 < final["bytes_read"] <= log_path.stat().st_size


def test_post_ingest_file_validates_required_fields(client: TestClient) -> None:
    missing_path = client.post("/_ingest/file", json={"index": "logs"})
    assert missing_path.status_code == 400

    missing_index = client.post("/_ingest/file", json={"path": "/tmp/whatever.log"})
    assert missing_index.status_code == 400


def test_post_ingest_file_rejects_a_bad_from_value(client: TestClient, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text("hello\n")
    resp = client.post(
        "/_ingest/file", json={"path": str(log_path), "index": "logs", "from": "yesterday"}
    )
    assert resp.status_code == 400
