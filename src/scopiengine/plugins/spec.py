"""Shapes a plugin module implements — one function per hook it wants to provide.

A plugin is an importable module exposing zero or more of the module-level
functions named below. None are required: a plugin that only wants to register
an analyzer defines ``register_analyzers`` and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from scopiengine.analysis.registry import AnalyzerRegistry
    from scopiengine.storage.factory import BackendFactory

__all__ = [
    "AnalyzerHook",
    "EventHook",
    "IngestContext",
    "IngestProcessor",
    "IngestProcessorHook",
    "StorageBackendHook",
]

#: ``process(doc, ctx) -> dict | None``. Returns the (possibly rewritten)
#: document, or ``None`` to drop it from ingestion entirely. The built-in
#: processors (``scopiengine.plugins.builtin.processors``: ``json_line``,
#: ``regex_extract``, ``timestamp``) and ``scopiengine.ingest.pipeline`` are
#: what actually calls into this contract during a real ingest run.
IngestContext = dict[str, Any]
IngestProcessor = Callable[[dict[str, Any], IngestContext], "dict[str, Any] | None"]


class AnalyzerHook(Protocol):
    """``register_analyzers(registry) -> None`` — add tokenizers/filters/analyzers."""

    def __call__(self, registry: AnalyzerRegistry) -> None: ...


class IngestProcessorHook(Protocol):
    """``register_ingest_processors() -> dict[str, IngestProcessor]``.

    Returns a mapping of processor name to callable, merged into the engine's
    processor registry.
    """

    def __call__(self) -> dict[str, IngestProcessor]: ...


class StorageBackendHook(Protocol):
    """``register_storage_backends(register) -> None``.

    ``register`` is :func:`scopiengine.storage.factory.register_backend` itself —
    plugins call it directly, the same way the built-in ``sqlite``/``mssql``
    schemes do, rather than going through a second registration mechanism.
    """

    def __call__(self, register: Callable[[str, BackendFactory], None]) -> None: ...


class EventHook(Protocol):
    """``register_events(bus) -> None`` — subscribe to lifecycle events.

    ``bus`` is the :class:`scopiengine.plugins.hooks.EventBus` the engine fires
    ``on_index_created`` / ``on_documents_indexed`` / ``on_search`` through.
    """

    def __call__(self, bus: Any) -> None: ...
