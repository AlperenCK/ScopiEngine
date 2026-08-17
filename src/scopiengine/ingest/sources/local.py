"""``LocalFileSource``: the only :class:`~scopiengine.ingest.sources.Source` this release ships.

Everything rotation- and truncation-aware in this module is built from two cheap
primitives: ``os.stat`` and a bounded read of the file's first few kilobytes — see
:func:`compute_signature`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from scopiengine.errors import IngestError
from scopiengine.ingest.sources import SourceStat

__all__ = ["SIGNATURE_HEAD_BYTES", "LocalFileSource", "compute_signature"]

#: Bytes hashed from the start of the file to build its signature. Large enough
#: that two unrelated log files essentially never collide, small enough that
#: signing a multi-gigabyte file is instant.
SIGNATURE_HEAD_BYTES = 4096


def compute_signature(path: Path, *, size: int) -> str:
    """Build a rotation-detecting signature: ``{st_dev}:{st_ino}:{sha1(head)}``.

    Two different files essentially never share a signature (different inode,
    almost certainly different leading bytes); the same file keeps the same
    signature across process restarts, which is what lets a resumed run tell
    "still the same file" apart from "rotated out from under me".
    """
    st = path.stat()
    with path.open("rb") as fh:
        head = fh.read(min(SIGNATURE_HEAD_BYTES, size) if size >= 0 else SIGNATURE_HEAD_BYTES)
    digest = hashlib.sha1(head).hexdigest()
    return f"{st.st_dev}:{st.st_ino}:{digest}"


class LocalFileSource:
    """A plain local file on the same filesystem the engine runs on.

    Args:
        path: Path to the file. Resolved to an absolute path immediately, so
            :attr:`uri` (and therefore the checkpoint key derived from it) does
            not change if the working directory later changes.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()

    @property
    def path(self) -> Path:
        """The resolved, absolute filesystem path."""
        return self._path

    @property
    def uri(self) -> str:
        return str(self._path)

    def exists(self) -> bool:
        """Whether the file currently exists."""
        return self._path.exists()

    def stat(self) -> SourceStat:
        try:
            st = self._path.stat()
            sig = compute_signature(self._path, size=st.st_size)
        except OSError as exc:
            raise IngestError(f"cannot stat source {self._path}: {exc}") from exc
        return SourceStat(size=st.st_size, sig=sig, mtime=st.st_mtime)

    def open(self, offset: int = 0) -> BinaryIO:
        try:
            fh = self._path.open("rb")
        except OSError as exc:
            raise IngestError(f"cannot open source {self._path}: {exc}") from exc
        if offset:
            fh.seek(offset)
        return fh
