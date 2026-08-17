"""The plugin's entry point: implements the ``analyzer`` and ``storage_backend`` hooks.

This module is exactly what ``examples/plugins/scopi_plugin_sample``'s
entry point (``scopiengine.plugins`` group, see ``pyproject.toml``) points at.
Nothing here is special-cased by ScopiEngine — it is loaded the same way the
first-party modules in ``scopiengine.plugins.builtin`` are.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from scopi_plugin_sample.dummy_backend import DummyBackend
from scopiengine.analysis.analyzer import Analyzer
from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.analysis.tokenizers import standard
from scopiengine.analysis.tokens import Token
from scopiengine.storage.base import StorageBackend
from scopiengine.storage.factory import BackendFactory

__all__ = ["register_analyzers", "register_storage_backends", "shout"]


def shout(tokens: Iterable[Token]) -> Iterator[Token]:
    """An UPPERCASE filter — the mirror image of the built-in ``lowercase``."""
    for token in tokens:
        yield Token(token.text.upper(), token.position)


def register_analyzers(registry: AnalyzerRegistry) -> None:
    """Register the ``shout`` filter and the analyzer built from it."""
    registry.register_filter("shout", shout)
    registry.register_analyzer("shout", Analyzer(standard, (shout,)))


def _dummy_factory(dsn: str, options: dict[str, object]) -> StorageBackend:
    return DummyBackend(dsn)


def register_storage_backends(register: Callable[[str, BackendFactory], None]) -> None:
    """Register the ``dummy://`` DSN scheme."""
    register("dummy", _dummy_factory)
