"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``SCOPI_*`` variable so the host environment cannot leak in."""
    import os

    for name in list(os.environ):
        if name.startswith("SCOPI_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def storage_dsn(tmp_path: Path) -> str:
    """A SQLite DSN pointing at a database inside the test's temporary directory."""
    return f"sqlite:///{tmp_path / 'scopi.db'}"
