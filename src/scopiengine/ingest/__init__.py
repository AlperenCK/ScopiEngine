"""Resilient, resumable log ingestion.

See ``docs/INGEST.md`` for the full picture. The short version:

```
LocalFileSource -> ChunkedByteReader -> RecordAssembler -> Queue(maxsize=queue_size)
  -> IngestWorker: processor chain -> analyze -> segment buffer
  -> [flush] write_segment + put_documents + save_checkpoint == ONE transaction
```

:class:`~scopiengine.ingest.pipeline.IngestPipeline` is the entry point both the
CLI (``scopi ingest file``) and the REST API (``POST /_ingest/file``, via
:class:`~scopiengine.ingest.jobs.IngestJobManager`) drive.
"""

from __future__ import annotations

from scopiengine.ingest.pipeline import IngestOptions, IngestPipeline, IngestStats

__all__ = ["IngestOptions", "IngestPipeline", "IngestStats"]
