"""``ChunkedByteReader``: streaming, bounded-memory line splitting over a binary stream.

Reads in fixed-size chunks (never the whole file), splits on ``b"\\n"``, and keeps
only the trailing partial line — never a whole file's worth of lines — buffered
between reads. A multi-byte UTF-8 character split across a chunk boundary decodes
correctly because decoding only ever happens on a *complete* line's bytes, built up
across as many chunks as it takes; the boundary lands inside the raw ``bytes``
buffer, never inside a call to ``.decode()``.

An over-long line (no ``\\n`` within ``max_line_bytes``) is truncated rather than
left to grow the buffer without bound — this is what keeps peak memory independent
of what is actually in the file, per line as well as overall.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

__all__ = ["DEFAULT_CHUNK_BYTES", "DEFAULT_MAX_LINE_BYTES", "ChunkedByteReader", "Line"]

#: Default read size per underlying ``stream.read()`` call.
DEFAULT_CHUNK_BYTES = 1024 * 1024

#: Default cap on a single line's byte length before it is truncated.
DEFAULT_MAX_LINE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Line:
    """One decoded physical line, with its absolute byte span in the source.

    Attributes:
        text: Decoded line content, ``\\r``/``\\n`` stripped. Decoded with
            ``errors="replace"``, so malformed bytes never raise.
        start: Absolute byte offset of the line's first byte in the source.
        end: Absolute byte offset immediately past the line's terminator (or
            past its last byte, for a final line with no trailing newline) —
            i.e. the offset the next line (if any) starts at.
        truncated: Whether the line exceeded ``max_line_bytes`` and was cut.
    """

    text: str
    start: int
    end: int
    truncated: bool


class ChunkedByteReader:
    """Splits a binary stream into :class:`Line` objects without materialising it.

    Args:
        stream: An already-open, already-positioned binary stream. The reader
            never seeks it; ``start_offset`` only tells the reader what
            absolute offset the stream's current position corresponds to, so
            the offsets it reports line up with the source's real byte
            positions after a resume.
        chunk_bytes: Bytes requested per underlying ``stream.read()`` call.
        max_line_bytes: A line longer than this is truncated to this length
            (bytes beyond the cap are read and counted but never buffered).
        start_offset: Absolute offset ``stream``'s current position represents.
    """

    def __init__(
        self,
        stream: BinaryIO,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        start_offset: int = 0,
    ) -> None:
        self._stream = stream
        self.chunk_bytes = chunk_bytes
        self.max_line_bytes = max_line_bytes
        self._offset = start_offset
        self._line_start = start_offset
        self._buffer = bytearray()
        self._overflow_extra = 0
        self._truncated_count = 0

    @property
    def offset(self) -> int:
        """Absolute byte offset consumed so far (the start of whatever comes next)."""
        return self._offset

    @property
    def truncated_line_count(self) -> int:
        """How many lines have been truncated for exceeding ``max_line_bytes``."""
        return self._truncated_count

    @property
    def pending_bytes(self) -> int:
        """Bytes currently buffered for an as-yet-incomplete line."""
        return len(self._buffer) + self._overflow_extra

    def read_available(self) -> Iterator[Line]:
        """Read whatever is currently available and yield every complete line found.

        Stops when a single ``stream.read(chunk_bytes)`` call returns no new
        bytes (a real EOF for a plain file, or "caught up for now" for a pipe
        or a file being tailed) — it never blocks waiting for more. Any
        trailing partial line stays buffered for the next call.
        """
        while True:
            chunk = self._stream.read(self.chunk_bytes)
            if not chunk:
                return
            yield from self._feed(chunk)

    def close_final(self) -> Line | None:
        """Flush a trailing line with no terminator, once the source is truly done.

        Call this only when no more bytes will ever arrive (a fully consumed,
        non-growing source) — never after a follow-mode "caught up for now"
        pause, which may still receive more bytes for the same line.
        """
        if not self._buffer and not self._overflow_extra:
            return None
        raw = bytes(self._buffer)
        self._buffer.clear()
        return self._finish_line(raw, consumed_extra=0)

    def _feed(self, chunk: bytes) -> Iterator[Line]:
        self._buffer.extend(chunk)
        while True:
            idx = self._buffer.find(b"\n")
            if idx == -1:
                overflow = len(self._buffer) - self.max_line_bytes
                if overflow > 0:
                    self._overflow_extra += overflow
                    del self._buffer[self.max_line_bytes :]
                return
            raw = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            yield self._finish_line(raw, consumed_extra=1)

    def _finish_line(self, raw: bytes, *, consumed_extra: int) -> Line:
        # A line can exceed the cap either by trickling in over many chunks
        # (``_overflow_extra`` already tracks the excess) or by arriving
        # whole, newline included, in a single ``_feed`` call (the newline is
        # found immediately, so ``_overflow_extra`` is still zero) - both
        # must be caught here, not just the first.
        total_content_len = self._overflow_extra + len(raw)
        truncated = total_content_len > self.max_line_bytes
        if truncated:
            self._truncated_count += 1
            if len(raw) > self.max_line_bytes:
                raw = raw[: self.max_line_bytes]
        self._overflow_extra = 0
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        start = self._line_start
        end = start + total_content_len + consumed_extra
        self._line_start = end
        self._offset = end
        return Line(
            text=raw.decode("utf-8", errors="replace"), start=start, end=end, truncated=truncated
        )
