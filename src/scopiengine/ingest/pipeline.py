"""``IngestPipeline``: wires reader, assembler, queue, processors and checkpointing together.

```
Source -> ChunkedByteReader -> RecordAssembler -> Queue(maxsize=queue_size)
  -> consumer: processor chain -> Batcher -> [flush] one storage transaction
```

The reader runs on its own thread and blocks on ``Queue.put`` once the queue
fills — that block is the entire backpressure mechanism: an indexer that falls
behind simply stops the reader from pulling more bytes off disk, which is what
keeps peak memory bounded by ``queue_size`` regardless of how large the source is.

The one guarantee this module exists to make structurally true, not just
documented: **a batch's checkpoint update is issued inside the same backend
transaction as that batch's segment and document writes** — see :meth:`_flush`.
Every backend's :meth:`~scopiengine.storage.base.StorageBackend.transaction` is
reentrant on one connection (nested calls join the outer transaction instead of
committing early), so wrapping ``engine.index_documents`` +
``engine.flush`` + ``storage.save_checkpoint`` in one ``with
storage.transaction():`` block is sufficient — no separate reconciliation path,
no "was this batch half-applied" check on startup, because a half-applied batch
is not a state the database can ever be left in.

Deliberately not called here: :meth:`~scopiengine.engine.Engine.refresh`'s
auto-merge check. Segment count matters for search speed, not ingest
correctness, and running it after every single batch would turn an
O(batches) ingest into an O(batches²) one (a merge rewrites the whole live
segment set every time it fires). Segments simply accumulate during a run;
``scopi index merge`` (or `refresh`, once) afterward is the documented way
to fold them back down — see docs/INGEST.md's tuning section.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from scopiengine.engine import Engine
from scopiengine.errors import IngestError
from scopiengine.ingest.assembler import Record, RecordAssembler
from scopiengine.ingest.batcher import Batcher
from scopiengine.ingest.checkpoint import (
    FromMode,
    advance,
    classify_change,
    compute_cp_key,
    decide_resume,
    new_checkpoint,
    short_sig,
)
from scopiengine.ingest.reader import DEFAULT_CHUNK_BYTES, DEFAULT_MAX_LINE_BYTES, ChunkedByteReader
from scopiengine.ingest.sources import Source
from scopiengine.plugins.builtin.processors import REGEX_PATTERN_CTX_KEY, TIMESTAMP_FIELDS_CTX_KEY
from scopiengine.plugins.spec import IngestContext, IngestProcessor
from scopiengine.storage.models import Checkpoint

__all__ = ["IdMode", "IngestOptions", "IngestPipeline", "IngestStats"]

_logger = logging.getLogger("scopiengine.ingest")

IdMode = Literal["offset", "content", "uuid"]

#: Adaptive follow-mode poll backoff: starts fast, doubles up to the ceiling
#: each empty poll, resets to the floor the moment new data shows up.
_POLL_MIN_INTERVAL = 0.1
_POLL_MAX_INTERVAL = 1.0


class _Sentinel:
    """Marks the reader thread's clean exit — a plain sentinel, not an error."""


_DONE = _Sentinel()


@dataclass(frozen=True, slots=True)
class _SigChange:
    """Tells the consumer the reader has crossed into a rotated-in file."""

    sig: str


@dataclass(frozen=True, slots=True)
class _ReaderFailure:
    """Carries a reader-thread exception across to the consumer thread."""

    exc: Exception


_QueueItem = Record | _Sentinel | _SigChange | _ReaderFailure


@dataclass(slots=True)
class IngestOptions:
    """Configuration for one ingestion run.

    Attributes:
        index: Target index name.
        processors: Named processors (resolved against
            ``engine.plugins.ingest_processors``), applied in order.
        multiline_start: Regex marking the start of a new record; folds
            non-matching lines into the previous one. ``None`` means one line = one record.
        follow: Keep polling for appended data after reaching EOF instead of exiting.
        from_mode: Where to start reading — see :func:`scopiengine.ingest.checkpoint.decide_resume`.
        id_mode: How external document ids are derived — see :meth:`IngestPipeline._make_doc_id`.
        batch_size: Override for ``Settings.batch_size``; ``None`` uses the engine's.
        batch_bytes: Override for ``Settings.batch_bytes``; ``None`` uses the engine's.
        flush_interval: Override for ``Settings.flush_interval``; ``None`` uses the engine's.
        queue_size: Override for ``Settings.queue_size``; ``None`` uses the engine's.
        chunk_bytes: Bytes per underlying read call.
        max_line_bytes: Cap on a single physical line before truncation.
        regex_pattern: Pattern for the ``regex_extract`` processor, if it is in ``processors``.
        timestamp_fields: Field-name search order for the ``timestamp`` processor, if used.
        poll_min_interval: Follow-mode poll floor, in seconds.
        poll_max_interval: Follow-mode poll ceiling, in seconds.
    """

    index: str
    processors: tuple[str, ...] = ()
    multiline_start: str | None = None
    follow: bool = False
    from_mode: FromMode = "start"
    id_mode: IdMode = "offset"
    batch_size: int | None = None
    batch_bytes: int | None = None
    flush_interval: float | None = None
    queue_size: int | None = None
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    regex_pattern: str | None = None
    timestamp_fields: tuple[str, ...] | None = None
    poll_min_interval: float = _POLL_MIN_INTERVAL
    poll_max_interval: float = _POLL_MAX_INTERVAL


@dataclass(slots=True)
class IngestStats:
    """A running snapshot of one ingestion run — read freely, mutated only by the consumer loop."""

    bytes_read: int = 0
    start_offset: int = 0
    file_size: int = 0
    records_read: int = 0
    docs_indexed: int = 0
    docs_dropped: int = 0
    batches_flushed: int = 0
    truncated_lines: int = 0
    queue_depth: int = 0
    segment_count: int = 0
    state: str = "running"
    error: str | None = None
    checkpoint: Checkpoint | None = None
    _started: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock time since the run started."""
        return time.monotonic() - self._started

    @property
    def bytes_per_second(self) -> float:
        """Throughput over the bytes *this run* read.

        A resumed run starts at the checkpoint's offset without having read any of
        the bytes before it, so dividing the absolute offset by this run's elapsed
        time would report a throughput the process never achieved — and an ETA far
        shorter than the work remaining.
        """
        elapsed = self.elapsed_seconds
        if elapsed <= 0:
            return 0.0
        return max(self.bytes_read - self.start_offset, 0) / elapsed

    @property
    def docs_per_second(self) -> float:
        elapsed = self.elapsed_seconds
        return self.docs_indexed / elapsed if elapsed > 0 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        """Estimated seconds to reach ``file_size``, or ``None`` when it cannot be estimated."""
        rate = self.bytes_per_second
        if rate <= 0 or self.file_size <= 0:
            return None
        remaining = self.file_size - self.bytes_read
        return max(remaining, 0) / rate


ProgressCallback = Callable[[IngestStats], None]


class IngestPipeline:
    """Runs one file's ingestion into one index, start to finish (or until stopped).

    Args:
        engine: The opened engine to index into and checkpoint against — its
            storage backend is what makes the transaction guarantee possible
            (see the module docstring).
        source: Where the bytes come from.
        options: Run configuration.
    """

    def __init__(self, engine: Engine, source: Source, options: IngestOptions) -> None:
        self.engine = engine
        self.storage = engine.storage
        self.source = source
        self.options = options
        settings = engine.settings
        self.batch_size = options.batch_size or settings.batch_size
        self.batch_bytes = options.batch_bytes or settings.batch_bytes
        self.flush_interval = options.flush_interval or settings.flush_interval
        self.queue_size = options.queue_size or settings.queue_size
        self.cp_key = compute_cp_key(options.index, source.uri)
        self._processors = self._resolve_processors()
        self._ctx: IngestContext = {
            "index": options.index,
            "source_uri": source.uri,
            "id_mode": options.id_mode,
        }
        if options.regex_pattern is not None:
            self._ctx[REGEX_PATTERN_CTX_KEY] = options.regex_pattern
        if options.timestamp_fields is not None:
            self._ctx[TIMESTAMP_FIELDS_CTX_KEY] = options.timestamp_fields

    def _resolve_processors(self) -> list[IngestProcessor]:
        available = self.engine.plugins.ingest_processors
        resolved = []
        for name in self.options.processors:
            processor = available.get(name)
            if processor is None:
                known = ", ".join(sorted(available)) or "(none registered)"
                raise IngestError(f"unknown ingest processor {name!r}; available: {known}")
            resolved.append(processor)
        return resolved

    # -- public entry point --------------------------------------------------

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
        progress: ProgressCallback | None = None,
        progress_interval: float = 1.0,
        stats: IngestStats | None = None,
    ) -> IngestStats:
        """Run ingestion to completion (or until ``stop_event`` is set).

        Args:
            stop_event: Set it from another thread to request a clean stop —
                the reader stops pulling new bytes, the queue drains, the
                current batch and checkpoint flush, then this returns with
                ``state == "stopped"``. A fresh, never-set event is used when omitted.
            progress: Called periodically (about every ``progress_interval``
                seconds) with the same live :class:`IngestStats` this method
                returns, so a caller can poll it mid-run too.
            progress_interval: Minimum seconds between ``progress`` calls.
            stats: An existing :class:`IngestStats` to fill in and return,
                instead of constructing a fresh one — lets a caller (see
                :mod:`scopiengine.ingest.jobs`) hold a reference that stays
                live for the whole run, not just from the first callback on.

        Returns:
            The final :class:`IngestStats`, with ``state`` one of ``"done"``
            (non-follow mode reached real EOF), ``"stopped"`` (``stop_event``
            was set — a clean, resumable stop) or ``"failed"`` (the reader hit
            an unrecoverable error; also raised as an :class:`IngestError`).

        Raises:
            IngestError: The source could not be read, or a configured
                processor name is not registered.
        """
        stop_event = stop_event if stop_event is not None else threading.Event()
        stat = self.source.stat()
        existing = self.storage.load_checkpoint(self.cp_key)
        decision = decide_resume(
            existing, current_sig=stat.sig, current_size=stat.size, from_mode=self.options.from_mode
        )
        if decision.reason in ("restart_rotated", "restart_truncated"):
            _logger.warning(
                "ingest %s: %s (checkpoint offset %s, current size %s) - restarting at byte 0",
                self.source.uri,
                decision.reason,
                existing.byte_offset if existing else None,
                stat.size,
            )
        if existing is None or decision.is_restart:
            checkpoint = new_checkpoint(
                cp_key=self.cp_key,
                index=self.options.index,
                source_uri=self.source.uri,
                sig=decision.sig,
                byte_offset=decision.start_offset,
            )
        else:
            checkpoint = advance(
                existing,
                byte_offset=decision.start_offset,
                sig=decision.sig,
                state="running",
                error=None,
            )
        self.storage.save_checkpoint(checkpoint)

        stats = stats if stats is not None else IngestStats()
        stats.bytes_read = checkpoint.byte_offset
        # Where this run begins, so throughput and ETA measure the work it actually
        # does rather than counting a resumed prefix it never read.
        stats.start_offset = checkpoint.byte_offset
        stats.file_size = stat.size
        stats.checkpoint = checkpoint
        stats.state = "running"
        stats._started = time.monotonic()

        work: queue.Queue[_QueueItem] = queue.Queue(maxsize=self.queue_size)
        reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(work, checkpoint.byte_offset, decision.sig, stop_event),
            name=f"scopi-ingest-reader:{self.options.index}",
            daemon=True,
        )
        reader_thread.start()
        try:
            self._consume(
                work, checkpoint, decision.sig, stats, stop_event, progress, progress_interval
            )
        finally:
            reader_thread.join()
        return stats

    # -- reader thread -----------------------------------------------------

    def _reader_loop(
        self,
        work: queue.Queue[_QueueItem],
        start_offset: int,
        start_sig: str,
        stop_event: threading.Event,
    ) -> None:
        try:
            handle = self.source.open(start_offset)
        except Exception as exc:
            work.put(_ReaderFailure(exc))
            return
        reader = ChunkedByteReader(
            handle,
            chunk_bytes=self.options.chunk_bytes,
            max_line_bytes=self.options.max_line_bytes,
            start_offset=start_offset,
        )
        assembler = RecordAssembler(multiline_start=self.options.multiline_start)
        running_sig = start_sig
        poll_interval = self.options.poll_min_interval
        try:
            while True:
                got_any = False
                for line in reader.read_available():
                    got_any = True
                    record = assembler.feed(line)
                    if record is not None:
                        work.put(record)
                    if stop_event.is_set():
                        break
                if stop_event.is_set():
                    break
                if not self.options.follow:
                    trailing = assembler.close()
                    if trailing is not None:
                        work.put(trailing)
                    break
                if got_any:
                    poll_interval = self.options.poll_min_interval
                else:
                    handle, reader, assembler, running_sig, poll_interval = self._poll_once(
                        handle, reader, assembler, work, running_sig, reader.offset, poll_interval
                    )
                if stop_event.wait(timeout=poll_interval):
                    break
            handle.close()
        except Exception as exc:
            work.put(_ReaderFailure(exc))
            return
        work.put(_DONE)

    def _poll_once(
        self,
        handle: Any,
        reader: ChunkedByteReader,
        assembler: RecordAssembler,
        work: queue.Queue[_QueueItem],
        running_sig: str,
        running_offset: int,
        poll_interval: float,
    ) -> tuple[Any, ChunkedByteReader, RecordAssembler, str, float]:
        """Re-stat for rotation/truncation and act; returns the (possibly new) reader state."""
        try:
            current = self.source.stat()
        except (OSError, IngestError):
            # Transient: e.g. a rotator's rename-then-create leaves nothing at
            # this path for a brief instant. Treated exactly like "unchanged"
            # for this poll - the file reappearing, or a real rotation, both
            # surface on a later poll instead.
            return (
                handle,
                reader,
                assembler,
                running_sig,
                min(poll_interval * 2, self.options.poll_max_interval),
            )

        change = classify_change(running_sig, running_offset, current.sig, current.size)
        if change == "unchanged":
            return (
                handle,
                reader,
                assembler,
                running_sig,
                min(poll_interval * 2, self.options.poll_max_interval),
            )

        if change == "rotated":
            _logger.info("ingest %s: rotated, draining old handle then reopening", self.source.uri)
            for line in reader.read_available():
                record = assembler.feed(line)
                if record is not None:
                    work.put(record)
            trailing = assembler.close()
            if trailing is not None:
                work.put(trailing)
            work.put(_SigChange(current.sig))
            next_sig = current.sig
        else:  # "truncated"
            _logger.warning("ingest %s: truncated in place, restarting at byte 0", self.source.uri)
            next_sig = running_sig

        handle.close()
        new_handle = self.source.open(0)
        new_reader = ChunkedByteReader(
            new_handle,
            chunk_bytes=self.options.chunk_bytes,
            max_line_bytes=self.options.max_line_bytes,
            start_offset=0,
        )
        new_assembler = RecordAssembler(multiline_start=self.options.multiline_start)
        return new_handle, new_reader, new_assembler, next_sig, self.options.poll_min_interval

    # -- consumer (this thread) --------------------------------------------

    def _consume(
        self,
        work: queue.Queue[_QueueItem],
        checkpoint: Checkpoint,
        current_sig: str,
        stats: IngestStats,
        stop_event: threading.Event,
        progress: ProgressCallback | None,
        progress_interval: float,
    ) -> None:
        batcher = Batcher(
            batch_size=self.batch_size,
            batch_bytes=self.batch_bytes,
            flush_interval=self.flush_interval,
        )
        last_progress = 0.0

        def _maybe_report() -> None:
            nonlocal last_progress
            stats.queue_depth = work.qsize()
            now = time.monotonic()
            if progress is not None and now - last_progress >= progress_interval:
                stats.segment_count = self.storage.index_stats(self.options.index).get(
                    "segment_count", 0
                )
                progress(stats)
                last_progress = now

        while True:
            wait = min(batcher.time_remaining, progress_interval)
            try:
                item = work.get(timeout=max(wait, 0.01))
            except queue.Empty:
                if batcher.should_flush:
                    checkpoint = self._flush(batcher, checkpoint, current_sig, stats)
                _maybe_report()
                continue

            if isinstance(item, _Sentinel):
                if batcher.has_pending:
                    checkpoint = self._flush(batcher, checkpoint, current_sig, stats)
                break
            if isinstance(item, _ReaderFailure):
                if batcher.has_pending:
                    checkpoint = self._flush(batcher, checkpoint, current_sig, stats)
                checkpoint = advance(
                    checkpoint,
                    byte_offset=checkpoint.byte_offset,
                    state="failed",
                    error=str(item.exc),
                )
                self.storage.save_checkpoint(checkpoint)
                stats.checkpoint = checkpoint
                stats.state = "failed"
                stats.error = str(item.exc)
                raise IngestError(f"ingest of {self.source.uri!r} failed: {item.exc}") from item.exc
            if isinstance(item, _SigChange):
                current_sig = item.sig
                continue

            self._handle_record(item, current_sig, batcher, stats)
            if batcher.should_flush:
                checkpoint = self._flush(batcher, checkpoint, current_sig, stats)
            _maybe_report()

        final_state = "stopped" if stop_event.is_set() else "done"
        checkpoint = advance(
            checkpoint, byte_offset=checkpoint.byte_offset, sig=current_sig, state=final_state
        )
        self.storage.save_checkpoint(checkpoint)
        stats.checkpoint = checkpoint
        stats.state = final_state
        if progress is not None:
            stats.segment_count = self.storage.index_stats(self.options.index).get(
                "segment_count", 0
            )
            progress(stats)

    def _handle_record(
        self, record: Record, current_sig: str, batcher: Batcher, stats: IngestStats
    ) -> None:
        stats.records_read += 1
        stats.bytes_read = max(stats.bytes_read, record.end_offset)
        if record.truncated:
            stats.truncated_lines += 1

        doc: dict[str, Any] | None = {"message": record.text}
        for processor in self._processors:
            assert doc is not None
            doc = processor(doc, self._ctx)
            if doc is None:
                break

        if doc is not None:
            doc_id = self._make_doc_id(current_sig, record)
            batcher.add_document(doc, doc_id, nbytes=len(record.text.encode("utf-8")))
            stats.docs_indexed += 1
        else:
            stats.docs_dropped += 1
        batcher.record_consumed(end_offset=record.end_offset)

    def _make_doc_id(self, sig: str, record: Record) -> str:
        mode = self.options.id_mode
        if mode == "uuid":
            return uuid.uuid4().hex
        if mode == "content":
            return hashlib.blake2b(record.text.encode("utf-8"), digest_size=10).hexdigest()
        return f"{short_sig(sig)}:{record.offset}"

    def _flush(
        self, batcher: Batcher, checkpoint: Checkpoint, current_sig: str, stats: IngestStats
    ) -> Checkpoint:
        """Write the batch's documents, its segment, and the advanced checkpoint atomically.

        This is the single-transaction guarantee, made structural rather than
        merely intended: every write below happens inside one
        ``with self.storage.transaction():`` block, and every backend's
        ``transaction()`` is reentrant on its one connection — the nested
        transactions ``engine.index_documents``/``engine.flush``/
        ``storage.save_checkpoint`` each open internally all join this outer
        one instead of committing early. A crash at any point before this
        block returns leaves the backend exactly as it was before the block
        started: no documents, no segment, no checkpoint advance. A crash
        after it returns leaves all three durably in place. There is no
        third state.
        """
        payload = batcher.take()
        if payload is None:
            return checkpoint
        with self.storage.transaction():
            if payload.docs:
                self.engine.index_documents(self.options.index, payload.docs, ids=payload.ids)
                self.engine.flush(self.options.index)
            checkpoint = advance(
                checkpoint,
                byte_offset=payload.end_offset,
                record_count_delta=payload.record_count,
                sig=current_sig,
                state="running",
            )
            self.storage.save_checkpoint(checkpoint)
        stats.batches_flushed += 1
        stats.checkpoint = checkpoint
        return checkpoint
