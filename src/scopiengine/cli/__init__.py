"""Command-line interface for ScopiEngine.

The CLI drives the engine in process rather than talking to a running server, so
every command works without ``scopi serve``.
"""

from __future__ import annotations

__all__ = ["run"]


def run() -> None:
    """Console-script entry point."""
    from scopiengine.cli.main import run as _run

    _run()
