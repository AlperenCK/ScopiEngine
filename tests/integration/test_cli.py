"""The CLI runs end to end through Typer's runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import scopiengine
from scopiengine.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_settings(isolated_env: None) -> None:
    """Keep the host environment out of every CLI test."""


def test_version_prints_the_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == scopiengine.__version__


def test_version_flag_is_eager() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == scopiengine.__version__


def test_version_as_json() -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "name": "scopiengine",
        "version": scopiengine.__version__,
    }


def test_info_shows_the_resolved_settings() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert scopiengine.TAGLINE in result.stdout
    assert "storage" in result.stdout


def test_info_as_json_carries_the_settings() -> None:
    result = runner.invoke(app, ["--json", "info"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == scopiengine.__version__
    assert payload["settings"]["port"] == scopiengine.DEFAULT_PORT


def test_storage_option_reaches_the_settings() -> None:
    result = runner.invoke(app, ["--json", "--storage", "sqlite:///./x.db", "info"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["settings"]["storage"] == "sqlite:///./x.db"


def test_verbose_sets_debug_logging() -> None:
    result = runner.invoke(app, ["--json", "-v", "info"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["settings"]["log_level"] == "DEBUG"


def test_quiet_sets_error_logging() -> None:
    result = runner.invoke(app, ["--json", "-q", "info"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["settings"]["log_level"] == "ERROR"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    # Click's convention for "no command given": print usage, exit 2.
    assert result.exit_code == 2
    assert "Usage" in result.stdout


def test_bad_setting_reports_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPI_PORT", "not-a-port")
    result = runner.invoke(app, ["info"])
    assert result.exit_code != 0


def test_storage_init_creates_the_schema(storage_dsn: str) -> None:
    result = runner.invoke(app, ["--storage", storage_dsn, "--json", "storage", "init"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["storage"] == storage_dsn
    assert payload["schema_version"] >= 1


def test_storage_info_reports_reachability(storage_dsn: str) -> None:
    result = runner.invoke(app, ["--storage", storage_dsn, "--json", "storage", "info"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["scheme"] == "sqlite"
    assert payload["reachable"] is True


def test_storage_migrate_is_idempotent(storage_dsn: str) -> None:
    first = runner.invoke(app, ["--storage", storage_dsn, "storage", "migrate"])
    second = runner.invoke(app, ["--storage", storage_dsn, "storage", "migrate"])
    assert first.exit_code == 0
    assert second.exit_code == 0


# -- index, doc, bulk, search, analyze, plugin ---------------------------------

_MAPPING_JSON = json.dumps(
    {
        "properties": {
            "level": {"type": "keyword"},
            "service": {"type": "keyword"},
            "status": {"type": "long"},
            "message": {"type": "text"},
        }
    }
)


def _create_logs_index(storage_dsn: str, tmp_path: Path) -> Path:
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(_MAPPING_JSON, encoding="utf-8")
    result = runner.invoke(
        app, ["--storage", storage_dsn, "index", "create", "logs", "--mapping", str(mapping_file)]
    )
    assert result.exit_code == 0, result.stdout
    return mapping_file


def test_index_create_list_show_delete(storage_dsn: str, tmp_path: Path) -> None:
    _create_logs_index(storage_dsn, tmp_path)

    listed = runner.invoke(app, ["--storage", storage_dsn, "--json", "index", "list"])
    assert listed.exit_code == 0
    names = [row["name"] for row in json.loads(listed.stdout)]
    assert names == ["logs"]

    shown = runner.invoke(app, ["--storage", storage_dsn, "--json", "index", "show", "logs"])
    assert shown.exit_code == 0
    assert "level" in json.loads(shown.stdout)["mapping"]["properties"]

    deleted = runner.invoke(app, ["--storage", storage_dsn, "index", "delete", "logs"])
    assert deleted.exit_code == 0

    listed_again = runner.invoke(app, ["--storage", storage_dsn, "--json", "index", "list"])
    assert json.loads(listed_again.stdout) == []


def test_index_create_already_exists_errors(storage_dsn: str, tmp_path: Path) -> None:
    # Matches the existing `test_bad_setting_reports_the_variable` convention:
    # `runner.invoke(app, ...)` calls the Typer app directly, not the `run()`
    # entry point that prints a ScopiError's message and maps it to exit code
    # 1 — so here the exception simply propagates, and only the exit code
    # (via CliRunner's own non-zero-on-exception handling) is asserted.
    from scopiengine.errors import IndexAlreadyExistsError

    _create_logs_index(storage_dsn, tmp_path)
    result = runner.invoke(app, ["--storage", storage_dsn, "index", "create", "logs"])
    assert result.exit_code != 0
    assert isinstance(result.exception, IndexAlreadyExistsError)


def test_doc_put_get_delete(storage_dsn: str, tmp_path: Path) -> None:
    _create_logs_index(storage_dsn, tmp_path)

    put = runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "doc",
            "put",
            "logs",
            '{"level": "INFO", "message": "hello"}',
            "--id",
            "1",
        ],
    )
    assert put.exit_code == 0, put.stdout

    got = runner.invoke(app, ["--storage", storage_dsn, "--json", "doc", "get", "logs", "1"])
    assert got.exit_code == 0
    assert json.loads(got.stdout)["_source"]["message"] == "hello"

    deleted = runner.invoke(app, ["--storage", storage_dsn, "doc", "delete", "logs", "1"])
    assert deleted.exit_code == 0

    missing = runner.invoke(app, ["--storage", storage_dsn, "doc", "get", "logs", "1"])
    assert missing.exit_code == 1


def test_bulk_then_search_with_no_server_running(storage_dsn: str, tmp_path: Path) -> None:
    """`scopi bulk` then `scopi search`, two separate process invocations, no server."""
    _create_logs_index(storage_dsn, tmp_path)

    bulk_file = tmp_path / "docs.ndjson"
    bulk_file.write_text(
        "\n".join(
            [
                json.dumps({"index": {"_id": "1"}}),
                json.dumps({"level": "ERROR", "service": "auth", "status": 500, "message": "boom"}),
                json.dumps({"index": {"_id": "2"}}),
                json.dumps({"level": "INFO", "service": "auth", "status": 200, "message": "ok"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bulk_result = runner.invoke(
        app, ["--storage", storage_dsn, "--json", "bulk", "logs", "--file", str(bulk_file)]
    )
    assert bulk_result.exit_code == 0, bulk_result.stdout
    assert json.loads(bulk_result.stdout)["errors"] is False

    # A brand new process, with no server running, must already see the bulk-loaded data.
    search_result = runner.invoke(
        app, ["--storage", storage_dsn, "--json", "search", "logs", "level:ERROR"]
    )
    assert search_result.exit_code == 0, search_result.stdout
    payload = json.loads(search_result.stdout)
    assert payload["hits"]["total"]["value"] == 1
    assert payload["hits"]["hits"][0]["_id"] == "1"


def test_search_with_dsl_file(storage_dsn: str, tmp_path: Path) -> None:
    _create_logs_index(storage_dsn, tmp_path)
    runner.invoke(
        app,
        ["--storage", storage_dsn, "doc", "put", "logs", '{"level": "ERROR"}', "--id", "1"],
    )

    dsl_file = tmp_path / "dsl.json"
    dsl_file.write_text(json.dumps({"query": {"term": {"level": "ERROR"}}}), encoding="utf-8")
    result = runner.invoke(
        app,
        ["--storage", storage_dsn, "--json", "search", "logs", "--dsl", str(dsl_file)],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["hits"]["total"]["value"] == 1


def test_search_rejects_both_query_and_dsl(storage_dsn: str, tmp_path: Path) -> None:
    _create_logs_index(storage_dsn, tmp_path)
    dsl_file = tmp_path / "dsl.json"
    dsl_file.write_text(json.dumps({"query": {"match_all": {}}}), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "search",
            "logs",
            "level:ERROR",
            "--dsl",
            str(dsl_file),
        ],
    )
    assert result.exit_code != 0


def test_search_explain_includes_the_compiled_query(storage_dsn: str, tmp_path: Path) -> None:
    _create_logs_index(storage_dsn, tmp_path)
    result = runner.invoke(
        app,
        ["--storage", storage_dsn, "search", "logs", "level:ERROR", "--explain"],
    )
    assert result.exit_code == 0
    assert '"term"' in result.stdout


def test_index_stats_refresh_and_merge(storage_dsn: str, tmp_path: Path) -> None:
    _create_logs_index(storage_dsn, tmp_path)
    runner.invoke(
        app, ["--storage", storage_dsn, "doc", "put", "logs", '{"level": "INFO"}', "--id", "1"]
    )

    stats = runner.invoke(app, ["--storage", storage_dsn, "--json", "index", "stats", "logs"])
    assert stats.exit_code == 0
    assert json.loads(stats.stdout)["live_doc_count"] == 1

    refreshed = runner.invoke(app, ["--storage", storage_dsn, "index", "refresh", "logs"])
    assert refreshed.exit_code == 0

    merged = runner.invoke(app, ["--storage", storage_dsn, "index", "merge", "logs"])
    assert merged.exit_code == 0


def test_analyze_command(storage_dsn: str) -> None:
    result = runner.invoke(
        app,
        [
            "--storage",
            storage_dsn,
            "--json",
            "analyze",
            "--analyzer",
            "standard",
            "Quick BROWN Fox",
        ],
    )
    assert result.exit_code == 0
    tokens = [t["token"] for t in json.loads(result.stdout)["tokens"]]
    assert tokens == ["quick", "brown", "fox"]


def test_plugin_list_command(storage_dsn: str) -> None:
    result = runner.invoke(app, ["--storage", storage_dsn, "--json", "plugin", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "scopiengine.plugins.builtin.analyzers" in payload["loaded"]
