"""``scopi ingest file|status|reset`` through the Typer ``CliRunner``.

The SIGINT test sends a genuine process signal (not a simulated stop) while a
`CliRunner.invoke()` call is running in this same process's main thread — the
same mechanism (and the same thread) a real terminal Ctrl-C would use, without
paying for a whole extra subprocess (the crash/resume drill in
``test_ingest_crash_resume.py`` already covers the subprocess/SIGKILL case).
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scopiengine.cli.main import app
from scopiengine.storage.factory import open_storage

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_settings(isolated_env: None) -> None:
    """Keep the host environment out of every CLI test (mirrors test_cli.py)."""


def _write_json_log(path: Path, n: int) -> None:
    lines = [json.dumps({"seq": i, "level": "ERROR" if i % 5 == 0 else "INFO"}) for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _create_index(storage_dsn: str) -> None:
    result = runner.invoke(app, ["--storage", storage_dsn, "index", "create", "logs"])
    assert result.exit_code == 0, result.stdout


def test_ingest_file_runs_to_completion_and_is_searchable(storage_dsn: str, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_json_log(log_path, 100)
    _create_index(storage_dsn)

    result = runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "--json",
            "ingest",
            "file",
            str(log_path),
            "--index",
            "logs",
            "--processor",
            "json_line",
            "--no-progress",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "done"
    assert payload["docs_indexed"] == 100
    assert payload["checkpoint"]["byte_offset"] == log_path.stat().st_size

    searched = runner.invoke(
        app, ["--storage", storage_dsn, "--json", "search", "logs", "level:ERROR"]
    )
    assert searched.exit_code == 0, searched.stdout
    assert json.loads(searched.stdout)["hits"]["total"]["value"] == 20


def test_ingest_file_rejects_a_missing_path(storage_dsn: str, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--storage", storage_dsn, "ingest", "file", str(tmp_path / "nope.log"), "--index", "logs"],
    )
    assert result.exit_code != 0


def test_ingest_file_rejects_an_unknown_processor(storage_dsn: str, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text("hello\n")
    _create_index(storage_dsn)
    result = runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "ingest",
            "file",
            str(log_path),
            "--index",
            "logs",
            "--processor",
            "totally_not_a_real_processor",
        ],
    )
    assert result.exit_code != 0


def test_ingest_status_and_reset(storage_dsn: str, tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    _write_json_log(log_path, 10)
    _create_index(storage_dsn)
    runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "ingest",
            "file",
            str(log_path),
            "--index",
            "logs",
            "--no-progress",
        ],
    )

    status = runner.invoke(app, ["--storage", storage_dsn, "--json", "ingest", "status"])
    assert status.exit_code == 0, status.stdout
    rows = json.loads(status.stdout)
    assert len(rows) == 1
    assert rows[0]["index_name"] == "logs"
    cp_key = rows[0]["cp_key"]

    reset = runner.invoke(app, ["--storage", storage_dsn, "--json", "ingest", "reset", cp_key])
    assert reset.exit_code == 0, reset.stdout

    status_after = runner.invoke(app, ["--storage", storage_dsn, "--json", "ingest", "status"])
    assert json.loads(status_after.stdout) == []


def test_ingest_reset_unknown_key_fails(storage_dsn: str) -> None:
    result = runner.invoke(app, ["--storage", storage_dsn, "ingest", "reset", "does-not-exist"])
    assert result.exit_code == 1


def _wait_for_progress(storage_dsn: str, *, min_offset: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with open_storage(storage_dsn) as backend:
            checkpoints = backend.list_checkpoints("logs")
            if checkpoints and checkpoints[0].byte_offset >= min_offset:
                return True
        time.sleep(0.02)
    return False


def test_sigint_during_ingest_exits_zero_with_a_resumable_checkpoint(
    storage_dsn: str, tmp_path: Path
) -> None:
    log_path = tmp_path / "app.log"
    _write_json_log(log_path, 20_000)
    _create_index(storage_dsn)

    def _send_sigint_once_progress_is_visible() -> None:
        _wait_for_progress(storage_dsn, min_offset=2000)
        os.kill(os.getpid(), signal.SIGINT)

    timer = threading.Thread(target=_send_sigint_once_progress_is_visible, daemon=True)
    timer.start()
    try:
        result = runner.invoke(
            app,
            [
                "--storage",
                storage_dsn,
                "--json",
                "ingest",
                "file",
                str(log_path),
                "--index",
                "logs",
                "--processor",
                "json_line",
                "--batch-size",
                "20",
                "--flush-interval",
                "30",
                "--no-progress",
            ],
        )
    finally:
        timer.join(timeout=5)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "stopped"
    assert 0 < payload["checkpoint"]["byte_offset"] < log_path.stat().st_size

    resumed = runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "--json",
            "ingest",
            "file",
            str(log_path),
            "--index",
            "logs",
            "--processor",
            "json_line",
            "--resume",
            "--no-progress",
        ],
    )
    assert resumed.exit_code == 0, resumed.stdout
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["state"] == "done"
    assert resumed_payload["checkpoint"]["byte_offset"] == log_path.stat().st_size

    stats = runner.invoke(app, ["--storage", storage_dsn, "--json", "index", "stats", "logs"])
    assert stats.exit_code == 0, stats.stdout
    assert json.loads(stats.stdout)["live_doc_count"] == 20_000
