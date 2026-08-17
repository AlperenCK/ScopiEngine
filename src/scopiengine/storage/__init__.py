"""Pluggable durable storage for indices, documents, postings and checkpoints.

The engine's inverted index lives entirely behind :class:`StorageBackend`, so a
SQLite database, an MS SQL Server database, or (from 1.1) MongoDB are equally
valid places to run ScopiEngine against. Build a backend with :func:`open_storage`
rather than importing a concrete backend class directly — that keeps optional
driver dependencies out of the import graph until they are actually needed.
"""

from __future__ import annotations

from scopiengine.storage.base import StorageBackend
from scopiengine.storage.factory import available_schemes, open_storage, register_backend
from scopiengine.storage.models import (
    Checkpoint,
    IndexInfo,
    NormsBlock,
    Posting,
    SegmentInfo,
    SegmentState,
    StoredDoc,
    TermStats,
)

__all__ = [
    "Checkpoint",
    "IndexInfo",
    "NormsBlock",
    "Posting",
    "SegmentInfo",
    "SegmentState",
    "StorageBackend",
    "StoredDoc",
    "TermStats",
    "available_schemes",
    "open_storage",
    "register_backend",
]
