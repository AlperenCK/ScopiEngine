"""``IndexManager``: create/get/delete/list indices, ingest, refresh, merge and search.

The one stateful thing this class owns beyond the storage backend is one
:class:`~scopiengine.index.writer.IndexWriter` per index it has written to —
kept around across calls so a buffer actually accumulates instead of flushing a
tiny segment on every :meth:`index_documents` call. Everything else (mapping,
segments, deletions) lives in storage and is re-read fresh each time, so two
:class:`IndexManager` instances pointed at the same backend never disagree
about anything except each other's unflushed buffers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import IngestError
from scopiengine.index.reader import IndexReader
from scopiengine.index.searcher import SearchResult
from scopiengine.index.searcher import search as run_search
from scopiengine.index.segments import force_merge as _merge_segments
from scopiengine.index.segments import should_auto_merge
from scopiengine.index.writer import IndexWriter
from scopiengine.mapping.dynamic import flatten_document, merge_dynamic
from scopiengine.mapping.mapping import Mapping, parse_mapping
from scopiengine.query.ast import Query
from scopiengine.storage.base import StorageBackend
from scopiengine.storage.models import IndexInfo, StoredDoc

__all__ = ["IndexManager"]

#: Prefix for the placeholder doc id a superseded ordinal is renamed to on
#: update — see the comment in `index_documents` for why this is necessary.
_RETIRED_DOC_ID_PREFIX = "\0scopi-retired-ord-"


class IndexManager:
    """Owns index lifecycle, ingestion, refresh/merge and search for one storage backend.

    Args:
        storage: The opened backend every index lives in.
        analyzers: Registry used to resolve ``text`` fields' analyzers.
        buffer_mb: Per-index write buffer size, in megabytes, before an
            automatic flush.
        max_segments: Live segment count that triggers an automatic merge on refresh.
    """

    def __init__(
        self,
        storage: StorageBackend,
        analyzers: AnalyzerRegistry,
        *,
        buffer_mb: int = 64,
        max_segments: int = 10,
    ) -> None:
        self.storage = storage
        self.analyzers = analyzers
        self.buffer_mb = buffer_mb
        self.max_segments = max_segments
        self._writers: dict[str, IndexWriter] = {}

    def _writer(self, name: str) -> IndexWriter:
        writer = self._writers.get(name)
        if writer is None:
            writer = IndexWriter(self.storage, name, buffer_mb=self.buffer_mb)
            self._writers[name] = writer
        return writer

    def _mapping(self, name: str) -> Mapping:
        return parse_mapping(self.storage.get_index(name).mapping)

    def _retire_existing(self, name: str, doc_ids: Sequence[str]) -> None:
        """Free up any of ``doc_ids`` that already resolve to a live ordinal.

        Re-indexing an existing id is "delete the old ordinal, append a new
        one" (see the module docstring) — but the ``documents`` table enforces
        one external id per row, and tombstoning
        (:meth:`~scopiengine.storage.base.StorageBackend.mark_deleted`) does not
        touch that row. Left alone, inserting the replacement under a fresh
        ordinal but the same external id would collide with the still-present
        old row. So the old row is renamed to an unreachable placeholder id
        first — freeing the real id for the new ordinal — and only then is the
        old ordinal tombstoned.
        """
        existing = self.storage.resolve_ids(name, list(doc_ids))
        if not existing:
            return
        retired = [
            StoredDoc(doc_ord=old_ord, doc_id=f"{_RETIRED_DOC_ID_PREFIX}{old_ord}", source=b"{}")
            for old_ord in existing.values()
        ]
        self.storage.put_documents(name, retired)
        self.storage.mark_deleted(name, list(existing.values()))

    # -- index lifecycle -----------------------------------------------------

    def create(
        self,
        name: str,
        mapping: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> IndexInfo:
        """Create an index, validating ``mapping`` if given.

        Raises:
            IndexAlreadyExistsError: An index named ``name`` already exists.
            MappingError: ``mapping`` is invalid.
        """
        parsed = parse_mapping(mapping if mapping is not None else {"properties": {}})
        return self.storage.create_index(name, parsed.to_dict(), settings or {})

    def get(self, name: str) -> IndexInfo:
        """Fetch one index's metadata.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        return self.storage.get_index(name)

    def exists(self, name: str) -> bool:
        """Whether an index named ``name`` exists."""
        try:
            self.storage.get_index(name)
        except Exception:
            return False
        return True

    def delete(self, name: str) -> None:
        """Delete an index and drop its cached writer, if any.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        self.storage.delete_index(name)
        self._writers.pop(name, None)

    def list_indices(self) -> list[IndexInfo]:
        """List every index, ordered by name."""
        return self.storage.list_indices()

    # -- ingestion -------------------------------------------------------------

    def index_documents(
        self, name: str, docs: Sequence[dict[str, Any]], ids: Sequence[str] | None = None
    ) -> list[str]:
        """Index a batch of documents, extending the mapping dynamically as needed.

        Args:
            name: Index name.
            docs: Raw JSON-like documents.
            ids: External document ids, one per doc; auto-generated (UUID4 hex)
                when omitted.

        Returns:
            The document id actually used for each doc, in order.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
            IngestError: ``ids`` was given but its length does not match ``docs``.
            MappingError: A document's value conflicts with an existing field's type.
        """
        if ids is not None and len(ids) != len(docs):
            raise IngestError(f"got {len(ids)} ids for {len(docs)} documents")
        if not docs:
            return []

        mapping = self._mapping(name)
        merged = mapping
        for doc in docs:
            merged = merge_dynamic(merged, doc)
        if merged.fields != mapping.fields:
            self.storage.update_mapping(name, merged.to_dict())
            mapping = merged

        doc_ids = list(ids) if ids is not None else [uuid.uuid4().hex for _ in docs]
        self._retire_existing(name, doc_ids)
        entries = [
            (doc_id, doc, flatten_document(doc)) for doc_id, doc in zip(doc_ids, docs, strict=True)
        ]
        self._writer(name).add_documents(entries, mapping=mapping, analyzers=self.analyzers)
        return doc_ids

    def get_document(self, name: str, doc_id: str) -> dict[str, Any] | None:
        """Fetch one live document by external id, or ``None`` if absent or deleted.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        resolved = self.storage.resolve_ids(name, [doc_id])
        doc_ord = resolved.get(doc_id)
        if doc_ord is None:
            return None
        if doc_ord in set(self.storage.iter_deleted(name)):
            return None
        found = self.storage.get_documents(name, [doc_ord])
        return json.loads(found[0].source) if found else None

    def delete_document(self, name: str, doc_id: str) -> bool:
        """Tombstone one document by external id.

        Returns:
            Whether a document with that id was found (and thus deleted).

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        resolved = self.storage.resolve_ids(name, [doc_id])
        doc_ord = resolved.get(doc_id)
        if doc_ord is None:
            return False
        self.storage.mark_deleted(name, [doc_ord])
        return True

    # -- refresh and merge -----------------------------------------------------

    def flush(self, name: str) -> int | None:
        """Flush the pending write buffer to a new segment, without checking auto-merge.

        This is what :meth:`refresh` does its first half of — split out because a
        caller that flushes very frequently on its own schedule (streaming
        ingestion, batch by batch) needs each document batch to become a
        durable segment without also paying to re-check (and potentially
        trigger) a full-index merge on every single batch, which would turn
        an O(batches) ingest into an O(batches²) one. Segments simply
        accumulate; a search still sees every one of them, just as more of
        them as separate segments rather than fewer, merged ones, until
        something calls :meth:`refresh` or :meth:`force_merge`.

        Returns:
            The newly flushed segment's id, or ``None`` when nothing was buffered.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        return self._writer(name).flush()

    def refresh(self, name: str) -> int | None:
        """Flush the pending write buffer to a new segment, then auto-merge if needed.

        Returns:
            The newly flushed segment's id, or ``None`` when nothing was buffered.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        segment_id = self.flush(name)
        if should_auto_merge(self.storage, name, max_segments=self.max_segments):
            _merge_segments(self.storage, name, self._mapping(name))
        return segment_id

    def force_merge(self, name: str) -> int | None:
        """Flush any pending buffer, then rewrite every live segment into one.

        Returns:
            The merged segment's id, or ``None`` when there was at most one
            live segment to merge.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        self._writer(name).flush()
        return _merge_segments(self.storage, name, self._mapping(name))

    # -- search and stats --------------------------------------------------

    def search(self, name: str, query: Query, *, size: int = 10, from_: int = 0) -> SearchResult:
        """Execute ``query`` against ``name``'s current, refreshed segments.

        Documents added since the last :meth:`refresh` are not yet visible —
        the same "refresh to search" boundary Lucene-family engines use.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
            InvalidQueryError: The query AST is malformed.
            UnsupportedFeatureError: A phrase clause targets a field without
                position tracking.
        """
        reader = IndexReader(self.storage, name, self._mapping(name))
        return run_search(reader, query, size=size, from_=from_)

    def stats(self, name: str) -> dict[str, Any]:
        """Summarise an index, including the currently unflushed buffer's size.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """
        info = dict(self.storage.index_stats(name))
        writer = self._writers.get(name)
        info["buffered_doc_count"] = writer.buffered_doc_count if writer else 0
        return info
