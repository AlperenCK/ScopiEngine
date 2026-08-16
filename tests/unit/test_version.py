"""The declared version must match the packaging metadata."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import scopiengine

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_version_is_semver() -> None:
    parts = scopiengine.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_version_matches_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == scopiengine.__version__


def test_version_matches_installed_distribution() -> None:
    assert metadata.version("scopiengine") == scopiengine.__version__


def test_author_is_declared_once() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["authors"] == [{"name": "Alperen CK", "email": "alprncck17@gmail.com"}]
    assert scopiengine.__author__ == "Alperen CK"
