"""Plain data carried across the storage boundary.

Every shape here is a frozen dataclass: storage backends build and return these,
never their own bespoke types, so the rest of the engine can stay backend-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

__all__ = [
    "Checkpoint",
    "IndexInfo",
    "NormsBlock",
    "Posting",
    "SegmentInfo",
    "SegmentState",
    "StoredDoc",
    "TermStats",
]


class SegmentState(IntEnum):
    """Lifecycle of an on-disk segment."""

    #: Visible to search and eligible to be merged.
    LIVE = 1
    #: Being folded into a new segment; still readable until the merge commits.
    MERGING = 2
    #: Superseded by a merge; kept only until :meth:`StorageBackend.drop_segments` runs.
    DEAD = 3


@dataclass(frozen=True, slots=True)
class Posting:
    """One term's occurrence in one document.

    Attributes:
        doc_ord: Document ordinal, relative to the segment's ``base_ord``.
        tf: Term frequency — how many times the term occurs in the document.
        positions: Token positions the term occurs at, ascending. Empty when the
            field was indexed without position tracking.
    """

    doc_ord: int
    tf: int
    positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StoredDoc:
    """A document as the storage layer sees it: an ordinal, an id and raw bytes.

    Attributes:
        doc_ord: Ordinal assigned at ingestion time, unique within the index.
        doc_id: External document id, unique within the index.
        source: The original document, serialised (typically JSON-encoded UTF-8).
    """

    doc_ord: int
    doc_id: str
    source: bytes


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    """Metadata for one on-disk segment.

    Attributes:
        segment_id: Segment ordinal, unique within the index.
        base_ord: First document ordinal covered by this segment.
        doc_count: Number of document ordinals covered, including deleted ones.
        state: Current lifecycle state.
        created_at: ISO-8601 UTC timestamp the segment was written.
    """

    segment_id: int
    base_ord: int
    doc_count: int
    state: SegmentState
    created_at: str


@dataclass(frozen=True, slots=True)
class TermStats:
    """Aggregate statistics for a term across every live segment.

    Attributes:
        term: The term text.
        doc_freq: Number of documents the term occurs in.
        total_tf: Sum of term frequencies across those documents.
    """

    term: str
    doc_freq: int
    total_tf: int


@dataclass(frozen=True, slots=True)
class NormsBlock:
    """Per-document field-length norms for one field within one segment.

    Attributes:
        field_id: Field the norms belong to.
        segment_id: Segment the norms belong to.
        base_ord: First document ordinal covered, matching the segment's ``base_ord``.
        doc_count: Number of document ordinals covered.
        total_length: Sum of field lengths, used to compute the average for BM25.
        norms: One clamped length byte per document ordinal, encoded with
            :func:`scopiengine.storage.codec.encode_norms`.
    """

    field_id: int
    segment_id: int
    base_ord: int
    doc_count: int
    total_length: int
    norms: bytes


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """Metadata for one index.

    Attributes:
        index_id: Backend-assigned surrogate key.
        name: User-facing index name, unique within the backend.
        mapping: Field mapping, as a plain JSON-serialisable dict.
        settings: Index-level settings, as a plain JSON-serialisable dict.
        doc_count: Number of live documents.
        next_ord: Next document ordinal :meth:`StorageBackend.allocate_ords` will hand out.
        next_segment: Next segment id :meth:`StorageBackend.allocate_segment` will hand out.
        created_at: ISO-8601 UTC timestamp the index was created.
    """

    index_id: int
    name: str
    mapping: dict[str, Any]
    settings: dict[str, Any]
    doc_count: int
    next_ord: int
    next_segment: int
    created_at: str


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Resume state for one ingestion source.

    Attributes:
        cp_key: Stable key identifying the checkpoint, unique within the backend.
        index_name: Index the checkpoint's ingestion writes into.
        source_uri: Location of the source being ingested (path, URL, ...).
        source_sig: Signature of the source (size, mtime, hash) used to detect rotation.
        byte_offset: Byte offset already consumed from the source.
        record_count: Records already ingested from the source.
        state: Free-form lifecycle label, e.g. ``running``, ``done``, ``failed``.
        error: Last error message, or ``None`` when there was none.
        started_at: ISO-8601 UTC timestamp the checkpoint was first created.
        updated_at: ISO-8601 UTC timestamp of the last update.
    """

    cp_key: str
    index_name: str
    source_uri: str
    source_sig: str
    byte_offset: int
    record_count: int
    state: str
    error: str | None
    started_at: str
    updated_at: str
