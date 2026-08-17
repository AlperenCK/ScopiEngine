"""The storage abstraction every backend implements.

The inverted index lives entirely behind this interface: nothing above it knows
whether postings sit in SQLite, MS SQL or (eventually) MongoDB. Every method is
batch-oriented — no per-term or per-document round trips — because the engine's
hot paths are bulk ingestion and bulk postings reads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from typing import Any

from scopiengine.storage.models import (
    Checkpoint,
    IndexInfo,
    NormsBlock,
    SegmentInfo,
    StoredDoc,
    TermStats,
)

__all__ = ["SegmentTermEntry", "StorageBackend"]

#: One term's encoded postings, ready to write into a segment: ``(field_id, term,
#: postings_blob, doc_freq, total_tf)``. ``postings_blob`` is produced by
#: :func:`scopiengine.storage.codec.encode_postings`.
SegmentTermEntry = tuple[int, str, bytes, int, int]


class StorageBackend(ABC):
    """Durable storage for indices, documents, the inverted index and checkpoints.

    Every mutating call outside of an explicit :meth:`transaction` block commits
    on its own; grouping several under one :meth:`transaction` makes them atomic.
    Implementations must support the context-manager protocol, calling
    :meth:`open` on entry and :meth:`close` on exit, so::

        with open_storage(dsn) as backend:
            backend.migrate()
    """

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Acquire the underlying connection. Safe to call more than once."""

    @abstractmethod
    def close(self) -> None:
        """Release the underlying connection. Safe to call more than once."""

    @abstractmethod
    def ping(self) -> bool:
        """Return whether the backend can currently serve requests."""

    @abstractmethod
    def migrate(self) -> None:
        """Create the schema if absent, or verify a compatible version if present.

        Raises:
            StorageError: The stored schema version is newer than this build
                understands.
        """

    @property
    @abstractmethod
    def schema_version(self) -> int:
        """The schema version this backend implementation writes."""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """Group a sequence of writes into one atomic transaction.

        Raises:
            StorageError: The transaction failed and was rolled back.
        """

    def __enter__(self) -> StorageBackend:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- indices -------------------------------------------------------------

    @abstractmethod
    def create_index(
        self, name: str, mapping: dict[str, Any], settings: dict[str, Any]
    ) -> IndexInfo:
        """Create a new, empty index.

        Raises:
            IndexAlreadyExistsError: An index named ``name`` already exists.
        """

    @abstractmethod
    def get_index(self, name: str) -> IndexInfo:
        """Fetch one index's metadata.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """

    @abstractmethod
    def delete_index(self, name: str) -> None:
        """Delete an index and everything stored beneath it.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """

    @abstractmethod
    def list_indices(self) -> list[IndexInfo]:
        """List every index, ordered by name."""

    @abstractmethod
    def update_mapping(self, name: str, mapping: dict[str, Any]) -> IndexInfo:
        """Replace an index's stored mapping.

        Raises:
            IndexNotFoundError: No index named ``name`` exists.
        """

    # -- ordinal and segment allocation ---------------------------------------

    @abstractmethod
    def allocate_ords(self, index: str, n: int) -> int:
        """Reserve ``n`` consecutive document ordinals.

        Returns:
            The first ordinal reserved; the caller owns ``[base_ord, base_ord + n)``.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def allocate_segment(self, index: str) -> int:
        """Reserve the next segment id for an index.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def resolve_field_ids(self, index: str, names: Sequence[str]) -> dict[str, int]:
        """Return each field's stable integer id, assigning new ids as needed.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    # -- documents -------------------------------------------------------------

    @abstractmethod
    def put_documents(self, index: str, docs: Iterable[StoredDoc]) -> None:
        """Insert or overwrite documents by ordinal.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def get_documents(self, index: str, ords: Sequence[int]) -> list[StoredDoc]:
        """Fetch stored documents by ordinal, silently skipping ordinals not present.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def resolve_ids(self, index: str, doc_ids: Sequence[str]) -> dict[str, int]:
        """Map external document ids to ordinals, omitting ids not present.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def mark_deleted(self, index: str, ords: Sequence[int]) -> None:
        """Tombstone document ordinals so search skips them until the next merge.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def iter_deleted(self, index: str) -> Iterator[int]:
        """Iterate every tombstoned ordinal for an index, ascending.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    # -- segments and postings -------------------------------------------------

    @abstractmethod
    def write_segment(
        self,
        index: str,
        segment_id: int,
        base_ord: int,
        doc_count: int,
        postings_iter: Iterable[SegmentTermEntry],
        norms: Iterable[NormsBlock],
    ) -> None:
        """Durably write one immutable segment.

        ``postings_iter`` is consumed in bounded chunks, never materialised in
        full, so a large segment does not require the whole term list in memory.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def list_segments(self, index: str) -> list[SegmentInfo]:
        """List every segment for an index, ordered by ``base_ord``.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def drop_segments(self, index: str, segment_ids: Sequence[int]) -> None:
        """Permanently remove segments and everything they wrote.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def get_postings(self, index: str, field_id: int, term: str) -> list[tuple[int, int, bytes]]:
        """Fetch a term's raw postings blob from every segment that has one.

        Returns:
            ``(segment_id, base_ord, postings_blob)`` triples, one per segment
            carrying the term. Decode each blob with
            :func:`scopiengine.storage.codec.decode_postings` and add ``base_ord``
            to translate ordinals back to absolute document ordinals.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def iter_terms(
        self,
        index: str,
        field_id: int,
        *,
        prefix: str | None = None,
        lo: str | None = None,
        hi: str | None = None,
    ) -> Iterator[str]:
        """Iterate distinct terms for a field in ascending order, via a range scan.

        Args:
            prefix: Restrict to terms starting with this string.
            lo: Restrict to terms ``>= lo``. Ignored when ``prefix`` is set.
            hi: Restrict to terms ``< hi``. Ignored when ``prefix`` is set.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def get_term_stats(self, index: str, field_id: int, term: str) -> TermStats:
        """Aggregate one term's document frequency and total term frequency.

        Returns:
            A :class:`~scopiengine.storage.models.TermStats` with zero counts when
            the term does not occur.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def get_norms(self, index: str, field_id: int, segment_id: int) -> NormsBlock | None:
        """Fetch one field's length norms for one segment, or ``None`` if absent.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    @abstractmethod
    def index_stats(self, index: str) -> dict[str, Any]:
        """Summarise an index: document, segment and deletion counts.

        Raises:
            IndexNotFoundError: No index named ``index`` exists.
        """

    # -- ingestion checkpoints ---------------------------------------------

    @abstractmethod
    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Insert or overwrite a checkpoint by its ``cp_key``."""

    @abstractmethod
    def load_checkpoint(self, cp_key: str) -> Checkpoint | None:
        """Fetch a checkpoint, or ``None`` if none is stored under that key."""

    @abstractmethod
    def list_checkpoints(self, index_name: str | None = None) -> list[Checkpoint]:
        """List checkpoints, optionally restricted to one index, newest first."""

    @abstractmethod
    def delete_checkpoint(self, cp_key: str) -> None:
        """Delete a checkpoint. A no-op when the key does not exist."""
