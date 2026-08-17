"""``Engine``: the single facade the REST API and the CLI both build on.

Opens storage from a DSN, then owns everything else — the
:class:`~scopiengine.index.manager.IndexManager`, the
:class:`~scopiengine.analysis.registry.AnalyzerRegistry` and the
:class:`~scopiengine.plugins.registry.PluginRegistry` — so a caller only ever
needs one object. PR 4 adds the ScopiQL and JSON-DSL parsers on top of
:meth:`Engine.search`, which already accepts a query AST
(:mod:`scopiengine.query.ast`) today.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scopiengine.analysis.analyzer import Analyzer
from scopiengine.index.manager import IndexManager
from scopiengine.index.searcher import SearchResult
from scopiengine.mapping.mapping import Mapping, parse_mapping
from scopiengine.plugins.registry import PluginRegistry, discover_plugins
from scopiengine.query.ast import Query
from scopiengine.settings import Settings
from scopiengine.storage.base import StorageBackend
from scopiengine.storage.factory import open_storage
from scopiengine.storage.models import IndexInfo

__all__ = ["Engine"]


class Engine:
    """The top-level entry point: one storage backend, fully wired up.

    Construct via :meth:`open` in normal use; the bare constructor is mainly
    useful for tests that already have a backend and registries assembled.
    """

    def __init__(
        self, storage: StorageBackend, plugins: PluginRegistry, *, settings: Settings
    ) -> None:
        self.storage = storage
        self.plugins = plugins
        self.settings = settings
        self.analyzers = plugins.analyzers
        self.manager = IndexManager(
            storage,
            self.analyzers,
            buffer_mb=settings.buffer_mb,
            max_segments=settings.max_segments,
        )

    @classmethod
    def open(cls, settings: Settings) -> Engine:
        """Open storage from ``settings.storage``, migrate it, and discover plugins.

        Raises:
            ConfigurationError: The DSN is malformed or names an unknown scheme.
            BackendNotAvailableError: The backend exists but is not usable here.
            PluginError: ``settings.strict_plugins`` is set and a plugin failed.
        """
        storage = open_storage(settings.storage)
        storage.open()
        storage.migrate()
        plugins = discover_plugins(settings)
        return cls(storage, plugins, settings=settings)

    def close(self) -> None:
        """Release the underlying storage connection."""
        self.storage.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- indices -------------------------------------------------------------

    def create_index(
        self,
        name: str,
        mapping: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> IndexInfo:
        """See :meth:`scopiengine.index.manager.IndexManager.create`."""
        info = self.manager.create(name, mapping, settings)
        self.plugins.events.fire("on_index_created", info)
        return info

    def delete_index(self, name: str) -> None:
        """See :meth:`scopiengine.index.manager.IndexManager.delete`."""
        self.manager.delete(name)

    def list_indices(self) -> list[IndexInfo]:
        """See :meth:`scopiengine.index.manager.IndexManager.list_indices`."""
        return self.manager.list_indices()

    def get_index(self, name: str) -> IndexInfo:
        """See :meth:`scopiengine.index.manager.IndexManager.get`."""
        return self.manager.get(name)

    def index_exists(self, name: str) -> bool:
        """See :meth:`scopiengine.index.manager.IndexManager.exists`."""
        return self.manager.exists(name)

    # -- documents -----------------------------------------------------------

    def index_documents(
        self, index: str, docs: Sequence[dict[str, Any]], ids: Sequence[str] | None = None
    ) -> list[str]:
        """See :meth:`scopiengine.index.manager.IndexManager.index_documents`."""
        doc_ids = self.manager.index_documents(index, docs, ids)
        self.plugins.events.fire("on_documents_indexed", index, doc_ids)
        return doc_ids

    def bulk(
        self, index: str, docs: Sequence[dict[str, Any]], ids: Sequence[str] | None = None
    ) -> list[str]:
        """Alias for :meth:`index_documents` — bulk ingestion is the normal path here."""
        return self.index_documents(index, docs, ids)

    def get_document(self, index: str, doc_id: str) -> dict[str, Any] | None:
        """See :meth:`scopiengine.index.manager.IndexManager.get_document`."""
        return self.manager.get_document(index, doc_id)

    def delete_document(self, index: str, doc_id: str) -> bool:
        """See :meth:`scopiengine.index.manager.IndexManager.delete_document`."""
        return self.manager.delete_document(index, doc_id)

    # -- refresh, merge and search -----------------------------------------

    def refresh(self, index: str) -> int | None:
        """See :meth:`scopiengine.index.manager.IndexManager.refresh`."""
        return self.manager.refresh(index)

    def force_merge(self, index: str) -> int | None:
        """See :meth:`scopiengine.index.manager.IndexManager.force_merge`."""
        return self.manager.force_merge(index)

    def search(self, index: str, query: Query, *, size: int = 10, from_: int = 0) -> SearchResult:
        """See :meth:`scopiengine.index.manager.IndexManager.search`.

        Returns:
            A :class:`~scopiengine.index.searcher.SearchResult` carrying both
            the requested page (``result.hits``) and the true total match
            count (``result.total``) — see the module note in
            :mod:`scopiengine.index.searcher` for how the total is computed.
        """
        result = self.manager.search(index, query, size=size, from_=from_)
        self.plugins.events.fire("on_search", index, query, result.hits)
        return result

    def stats(self, index: str) -> dict[str, Any]:
        """See :meth:`scopiengine.index.manager.IndexManager.stats`."""
        return self.manager.stats(index)

    def get_mapping(self, index: str) -> Mapping:
        """Fetch and parse one index's current field mapping.

        A convenience for the query compiler and the REST/CLI layers, which
        need the parsed :class:`~scopiengine.mapping.mapping.Mapping` (not the
        raw dict :meth:`get_index` returns) to resolve field types before
        building a query AST.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """
        return parse_mapping(self.get_index(index).mapping)

    # -- analysis --------------------------------------------------------------

    def analyze(self, text: str, *, analyzer: str = "standard") -> list[tuple[str, int]]:
        """Run a named analyzer over ``text``, returning ``(term, position)`` pairs.

        Raises:
            ConfigurationError: No analyzer is registered under ``analyzer``.
        """
        resolved: Analyzer = self.analyzers.analyzer(analyzer)
        return list(resolved.analyze(text))
