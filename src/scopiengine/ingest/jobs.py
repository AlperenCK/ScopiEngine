"""``IngestJobManager``: background-thread ingestion jobs for the REST API.

Only the REST layer needs job tracking — the CLI runs ingestion in the
foreground and *is* the job, exiting when it finishes. ``POST /_ingest/file``
instead has to return immediately, so this module starts each job on its own
daemon thread and hands back an id that ``GET /_ingest/jobs/{id}`` can poll.

Every job's :class:`~scopiengine.ingest.pipeline.IngestStats` is threaded
straight into :meth:`~scopiengine.ingest.pipeline.IngestPipeline.run` (its
``stats=`` argument), so a poller always sees the one live object the pipeline
is mutating — never a stale snapshot captured only when a progress callback
happened to fire.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from scopiengine.engine import Engine
from scopiengine.ingest.pipeline import IngestOptions, IngestPipeline, IngestStats
from scopiengine.ingest.sources.local import LocalFileSource

__all__ = ["IngestJob", "IngestJobManager"]


@dataclass(slots=True)
class IngestJob:
    """One background ingestion job.

    Attributes:
        job_id: Opaque id, unique within this process.
        index: Target index.
        source_uri: Resolved source path.
        stop_event: Set to request a clean stop (see
            :meth:`~scopiengine.ingest.pipeline.IngestPipeline.run`).
        stats: Live, continuously-updated stats — safe to read from any thread.
        thread: The worker thread running the pipeline.
        created_at: Unix timestamp the job was created.
    """

    job_id: str
    index: str
    source_uri: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    stats: IngestStats = field(default_factory=IngestStats)
    thread: threading.Thread | None = field(default=None, repr=False)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Render the job as a JSON-serialisable status document."""
        stats = self.stats
        return {
            "job_id": self.job_id,
            "index": self.index,
            "source": self.source_uri,
            "state": stats.state,
            "bytes_read": stats.bytes_read,
            "file_size": stats.file_size,
            "records_read": stats.records_read,
            "docs_indexed": stats.docs_indexed,
            "docs_dropped": stats.docs_dropped,
            "batches_flushed": stats.batches_flushed,
            "truncated_lines": stats.truncated_lines,
            "queue_depth": stats.queue_depth,
            "segment_count": stats.segment_count,
            "bytes_per_second": round(stats.bytes_per_second, 2),
            "docs_per_second": round(stats.docs_per_second, 2),
            "eta_seconds": stats.eta_seconds,
            "error": stats.error,
            "checkpoint_key": stats.checkpoint.cp_key if stats.checkpoint else None,
        }


class IngestJobManager:
    """Tracks background ingestion jobs for one open :class:`~scopiengine.engine.Engine`.

    Args:
        engine: The engine every job's :class:`~scopiengine.ingest.pipeline.IngestPipeline`
            runs against. Held for the manager's whole lifetime — jobs share it the
            same way concurrent CLI/REST callers would share one running server.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._jobs: dict[str, IngestJob] = {}
        self._lock = threading.Lock()

    def start(self, *, path: str, index: str, options: IngestOptions) -> IngestJob:
        """Start ingesting ``path`` on a background thread and return its job record."""
        job_id = uuid.uuid4().hex[:12]
        source = LocalFileSource(path)
        job = IngestJob(job_id=job_id, index=index, source_uri=source.uri)
        pipeline = IngestPipeline(self._engine, source, options)

        def _run() -> None:
            try:
                pipeline.run(stop_event=job.stop_event, stats=job.stats)
            except Exception as exc:
                job.stats.state = "failed"
                job.stats.error = str(exc)

        thread = threading.Thread(target=_run, name=f"scopi-ingest-job:{job_id}", daemon=True)
        job.thread = thread
        with self._lock:
            self._jobs[job_id] = job
        thread.start()
        return job

    def get(self, job_id: str) -> IngestJob | None:
        """Fetch one job by id, or ``None`` if unknown."""
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[IngestJob]:
        """List every job this manager has started, oldest first."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at)

    def stop(self, job_id: str) -> IngestJob | None:
        """Request a clean stop for a job, or ``None`` if it is unknown."""
        job = self.get(job_id)
        if job is not None:
            job.stop_event.set()
        return job
