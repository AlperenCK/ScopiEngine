"""The CLI runs end to end through Typer's runner."""

from __future__ import annotations

import json

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
