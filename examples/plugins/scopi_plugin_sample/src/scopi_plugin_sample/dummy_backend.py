"""``DummyBackend``: a fully in-memory :class:`~scopiengine.storage.base.StorageBackend`.

Exists purely to demonstrate the ``storage_backend`` plugin hook end to end —
it is registered under the ``dummy://`` DSN scheme, holds nothing on disk, and
loses all data the moment it is closed. Every method mirrors the semantics the
storage conformance suite (``tests/contract/test_storage_conformance.py`` in the
main ``scopiengine`` package) checks against the real backends, just backed by
plain Python dicts instead of a database connection.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from scopiengine.errors import IndexAlreadyExistsError, IndexNotFoundError
from scopiengine.storage.base import SegmentTermEntry, StorageBackend
from scopiengine.storage.models import (
    Checkpoint,
    IndexInfo,
    NormsBlock,
    SegmentInfo,
    SegmentState,
    StoredDoc,
    TermStats,
)

__all__ = ["DummyBackend"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class _IndexRecord:
    index_id: int
    name: str
    mapping: dict[str, Any]
    settings: dict[str, Any]
    created_at: str
    doc_count: int = 0
    next_ord: int = 0
    next_segment: int = 0
    field_ids: dict[str, int] = field(default_factory=dict)
    documents: dict[int, StoredDoc] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    segments: dict[int, SegmentInfo] = field(default_factory=dict)
    #: (field_id, term, segment_id) -> (postings_blob, doc_freq, total_tf)
    terms: dict[tuple[int, str, int], tuple[bytes, int, int]] = field(default_factory=dict)
    #: (field_id, segment_id) -> NormsBlock
    norms: dict[tuple[int, int], NormsBlock] = field(default_factory=dict)
    deleted: set[int] = field(default_factory=set)

    def to_info(self) -> IndexInfo:
        return IndexInfo(
            index_id=self.index_id,
            name=self.name,
            mapping=self.mapping,
            settings=self.settings,
            doc_count=self.doc_count,
            next_ord=self.next_ord,
            next_segment=self.next_segment,
            created_at=self.created_at,
        )


class DummyBackend(StorageBackend):
    """An in-memory storage backend, for demonstration and testing only.

    Args:
        dsn: The DSN it was constructed from — kept only for :meth:`ping`
            diagnostics, never parsed for configuration.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._open = False
        self._indices: dict[str, _IndexRecord] = {}
        self._next_index_id = 1
        self._checkpoints: dict[str, Checkpoint] = {}

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def ping(self) -> bool:
        return self._open

    def migrate(self) -> None:
        pass  # nothing to migrate — the in-memory shape is always current

    @property
    def schema_version(self) -> int:
        return 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # A pure Python dict mutation either completes or raises with nothing
        # partially visible in between, so there is no rollback to perform —
        # this only exists to satisfy the context-manager contract.
        yield

    # -- indices ---------------------------------------------------------------

    def _require(self, name: str) -> _IndexRecord:
        record = self._indices.get(name)
        if record is None:
            raise IndexNotFoundError(f"no such index: {name!r}")
        return record

    def create_index(
        self, name: str, mapping: dict[str, Any], settings: dict[str, Any]
    ) -> IndexInfo:
        if name in self._indices:
            raise IndexAlreadyExistsError(f"index already exists: {name!r}")
        record = _IndexRecord(
            index_id=self._next_index_id,
            name=name,
            mapping=mapping,
            settings=settings,
            created_at=_now_iso(),
        )
        self._next_index_id += 1
        self._indices[name] = record
        return record.to_info()

    def get_index(self, name: str) -> IndexInfo:
        return self._require(name).to_info()

    def delete_index(self, name: str) -> None:
        self._require(name)
        del self._indices[name]

    def list_indices(self) -> list[IndexInfo]:
        return [r.to_info() for r in sorted(self._indices.values(), key=lambda r: r.name)]

    def update_mapping(self, name: str, mapping: dict[str, Any]) -> IndexInfo:
        record = self._require(name)
        record.mapping = mapping
        return record.to_info()

    # -- ordinal and segment allocation ---------------------------------------

    def allocate_ords(self, index: str, n: int) -> int:
        record = self._require(index)
        base = record.next_ord
        record.next_ord += n
        return base

    def allocate_segment(self, index: str) -> int:
        record = self._require(index)
        segment_id = record.next_segment
        record.next_segment += 1
        return segment_id

    def resolve_field_ids(self, index: str, names: Sequence[str]) -> dict[str, int]:
        record = self._require(index)
        result: dict[str, int] = {}
        for name in names:
            field_id = record.field_ids.get(name)
            if field_id is None:
                field_id = len(record.field_ids)
                record.field_ids[name] = field_id
            result[name] = field_id
        return result

    # -- documents -----------------------------------------------------------

    def put_documents(self, index: str, docs: Iterable[StoredDoc]) -> None:
        record = self._require(index)
        for doc in docs:
            if doc.doc_ord not in record.documents:
                record.doc_count += 1
            record.documents[doc.doc_ord] = doc
            record.ids[doc.doc_id] = doc.doc_ord

    def get_documents(self, index: str, ords: Sequence[int]) -> list[StoredDoc]:
        record = self._require(index)
        return [record.documents[o] for o in ords if o in record.documents]

    def resolve_ids(self, index: str, doc_ids: Sequence[str]) -> dict[str, int]:
        record = self._require(index)
        return {doc_id: record.ids[doc_id] for doc_id in doc_ids if doc_id in record.ids}

    def mark_deleted(self, index: str, ords: Sequence[int]) -> None:
        record = self._require(index)
        record.deleted.update(ords)

    def iter_deleted(self, index: str) -> Iterator[int]:
        record = self._require(index)
        return iter(sorted(record.deleted))

    # -- segments and postings -------------------------------------------------

    def write_segment(
        self,
        index: str,
        segment_id: int,
        base_ord: int,
        doc_count: int,
        postings_iter: Iterable[SegmentTermEntry],
        norms: Iterable[NormsBlock],
    ) -> None:
        record = self._require(index)
        record.segments[segment_id] = SegmentInfo(
            segment_id=segment_id,
            base_ord=base_ord,
            doc_count=doc_count,
            state=SegmentState.LIVE,
            created_at=_now_iso(),
        )
        for field_id, term, blob, doc_freq, total_tf in postings_iter:
            record.terms[field_id, term, segment_id] = (blob, doc_freq, total_tf)
        for block in norms:
            record.norms[block.field_id, segment_id] = block

    def list_segments(self, index: str) -> list[SegmentInfo]:
        record = self._require(index)
        return sorted(record.segments.values(), key=lambda s: s.base_ord)

    def drop_segments(self, index: str, segment_ids: Sequence[int]) -> None:
        record = self._require(index)
        ids = set(segment_ids)
        for segment_id in ids:
            record.segments.pop(segment_id, None)
        record.terms = {k: v for k, v in record.terms.items() if k[2] not in ids}
        record.norms = {k: v for k, v in record.norms.items() if k[1] not in ids}

    def get_postings(self, index: str, field_id: int, term: str) -> list[tuple[int, int, bytes]]:
        record = self._require(index)
        results = []
        for (f_id, t, segment_id), (blob, _df, _tf) in record.terms.items():
            if f_id == field_id and t == term and segment_id in record.segments:
                segment = record.segments[segment_id]
                results.append((segment_id, segment.base_ord, blob))
        return results

    def iter_terms(
        self,
        index: str,
        field_id: int,
        *,
        prefix: str | None = None,
        lo: str | None = None,
        hi: str | None = None,
    ) -> Iterator[str]:
        record = self._require(index)
        terms = {
            t
            for (f_id, t, seg_id) in record.terms
            if f_id == field_id and seg_id in record.segments
        }
        if prefix is not None:
            terms = {t for t in terms if t.startswith(prefix)}
        else:
            if lo is not None:
                terms = {t for t in terms if t >= lo}
            if hi is not None:
                terms = {t for t in terms if t < hi}
        return iter(sorted(terms))

    def get_term_stats(self, index: str, field_id: int, term: str) -> TermStats:
        record = self._require(index)
        doc_freq = 0
        total_tf = 0
        for (f_id, t, segment_id), (_blob, df, tf) in record.terms.items():
            if f_id == field_id and t == term and segment_id in record.segments:
                doc_freq += df
                total_tf += tf
        return TermStats(term=term, doc_freq=doc_freq, total_tf=total_tf)

    def get_norms(self, index: str, field_id: int, segment_id: int) -> NormsBlock | None:
        record = self._require(index)
        return record.norms.get((field_id, segment_id))

    def index_stats(self, index: str) -> dict[str, Any]:
        record = self._require(index)
        deleted = len(record.deleted)
        return {
            "index": index,
            "doc_count": record.doc_count,
            "deleted_count": deleted,
            "live_doc_count": max(record.doc_count - deleted, 0),
            "segment_count": len(record.segments),
            "next_ord": record.next_ord,
            "next_segment": record.next_segment,
        }

    # -- ingestion checkpoints ---------------------------------------------

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.cp_key] = checkpoint

    def load_checkpoint(self, cp_key: str) -> Checkpoint | None:
        return self._checkpoints.get(cp_key)

    def list_checkpoints(self, index_name: str | None = None) -> list[Checkpoint]:
        values = list(self._checkpoints.values())
        if index_name is not None:
            values = [c for c in values if c.index_name == index_name]
        return sorted(values, key=lambda c: c.updated_at, reverse=True)

    def delete_checkpoint(self, cp_key: str) -> None:
        self._checkpoints.pop(cp_key, None)
