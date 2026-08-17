"""``Batcher``: accumulates processed documents until a flush trigger fires.

Three independent triggers, whichever fires first: a document count
(``batch_size``), an accumulated document byte size (``batch_bytes``), or a wall-clock
deadline (``flush_interval``) — the last one is what keeps ``--follow`` mode's
visible lag bounded even while a batch is far short of ``batch_size``.

A *record* is "consumed" the moment it is read and assembled, whether or not a
processor kept it as a document — the checkpoint has to advance past a dropped
record too, or a resume would read it again. :meth:`Batcher.record_consumed` and
:meth:`Batcher.add_document` are therefore separate calls: every record advances the
pending checkpoint offset; only a kept one also becomes part of the document batch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["BatchPayload", "Batcher"]


@dataclass(frozen=True, slots=True)
class BatchPayload:
    """One flush's worth of work: the documents to write and the offset to checkpoint.

    Attributes:
        docs: Documents to index, in arrival order.
        ids: External ids, one per entry in ``docs``, same order.
        end_offset: Byte offset to advance the checkpoint to — past every
            record folded into this batch, kept or dropped alike.
        record_count: Number of records folded into this batch.
    """

    docs: list[dict[str, Any]]
    ids: list[str]
    end_offset: int
    record_count: int


@dataclass(slots=True)
class Batcher:
    """Mutable accumulator; see the module docstring for the flush triggers.

    Args:
        batch_size: Flush once at least this many documents are buffered.
        batch_bytes: Flush once buffered documents' encoded size reaches this many bytes.
        flush_interval: Flush once this many seconds have passed since the last flush.
    """

    batch_size: int
    batch_bytes: int
    flush_interval: float
    _docs: list[dict[str, Any]] = field(default_factory=list, init=False)
    _ids: list[str] = field(default_factory=list, init=False)
    _doc_bytes: int = field(default=0, init=False)
    _pending_offset: int | None = field(default=None, init=False)
    _pending_records: int = field(default=0, init=False)
    _deadline: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._reset_deadline()

    def record_consumed(self, *, end_offset: int) -> None:
        """Advance the pending checkpoint offset for one record, kept or dropped."""
        self._pending_offset = end_offset
        self._pending_records += 1

    def add_document(self, doc: dict[str, Any], doc_id: str, *, nbytes: int) -> None:
        """Add one kept document to the batch."""
        self._docs.append(doc)
        self._ids.append(doc_id)
        self._doc_bytes += nbytes

    @property
    def doc_count(self) -> int:
        """Documents currently buffered."""
        return len(self._docs)

    @property
    def has_pending(self) -> bool:
        """Whether any record has been consumed since the last flush (docs or not)."""
        return self._pending_offset is not None

    @property
    def should_flush(self) -> bool:
        """Whether a flush trigger has fired for what is currently buffered."""
        if self._pending_offset is None:
            return False
        return (
            len(self._docs) >= self.batch_size
            or self._doc_bytes >= self.batch_bytes
            or time.monotonic() >= self._deadline
        )

    @property
    def time_remaining(self) -> float:
        """Seconds until the time-based trigger fires, floored at zero."""
        return max(0.0, self._deadline - time.monotonic())

    def take(self) -> BatchPayload | None:
        """Remove and return everything buffered, resetting for the next batch.

        Returns:
            ``None`` when nothing has been consumed since the last flush.
        """
        if self._pending_offset is None:
            return None
        payload = BatchPayload(
            docs=self._docs,
            ids=self._ids,
            end_offset=self._pending_offset,
            record_count=self._pending_records,
        )
        self._docs = []
        self._ids = []
        self._doc_bytes = 0
        self._pending_offset = None
        self._pending_records = 0
        self._reset_deadline()
        return payload

    def _reset_deadline(self) -> None:
        self._deadline = time.monotonic() + self.flush_interval
