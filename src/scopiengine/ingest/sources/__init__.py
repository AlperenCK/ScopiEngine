"""The source seam: what byte stream ingestion reads from.

:class:`Source` is a small ``Protocol`` — ``open(offset) -> BinaryIO`` plus
``stat() -> SourceStat`` — deliberately narrow so a future remote source
(SFTP, SSH, syslog, S3) slots in without touching :mod:`scopiengine.ingest.pipeline`
or anything above it. :class:`~scopiengine.ingest.sources.local.LocalFileSource`
is the only implementation this release ships; nothing here says "local file"
outside that one module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

__all__ = ["Source", "SourceStat"]


@dataclass(frozen=True, slots=True)
class SourceStat:
    """A cheap snapshot of a source's identity and extent.

    Attributes:
        size: Current size in bytes.
        sig: Stable signature used to detect rotation/replacement — see
            :mod:`scopiengine.ingest.checkpoint` for how it is built and compared.
        mtime: Last-modified time, informational only (not part of ``sig``).
    """

    size: int
    sig: str
    mtime: float


@runtime_checkable
class Source(Protocol):
    """What the ingest pipeline needs from anything it reads.

    Every method may be called repeatedly and cheaply — :meth:`stat` in
    particular is polled on every follow-mode tick, so an implementation must
    not do anything expensive (a full read, a network round trip heavier than
    a metadata call) inside it.
    """

    @property
    def uri(self) -> str:
        """A stable, human-readable identifier for this source (e.g. an absolute path)."""
        ...

    def stat(self) -> SourceStat:
        """Return a fresh :class:`SourceStat`.

        Raises:
            OSError: The source is currently unreachable (e.g. the file was removed).
        """
        ...

    def open(self, offset: int = 0) -> BinaryIO:
        """Open a fresh binary stream, positioned at ``offset`` bytes from the start.

        Raises:
            OSError: The source cannot be opened.
        """
        ...
