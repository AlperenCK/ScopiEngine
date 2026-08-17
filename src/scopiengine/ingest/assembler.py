"""``RecordAssembler``: folds physical lines into logical records.

One line is one record by default. With ``multiline_start`` set, a line is the
start of a new record only when it matches the pattern (or when nothing has been
buffered yet); every other line is folded into the record currently being built —
the classic "stack trace continuation" shape. The record in progress is only
*returned* once its end is known: either a following start-line arrives (proving
the previous group is complete) or the caller explicitly calls :meth:`close` once
the source itself is exhausted. A group that is still open when the pipeline stops
(follow-mode pause, SIGINT, a crash) is simply never returned — its bytes stay
unconsumed as far as the checkpoint is concerned, so a resume re-reads and
re-assembles it from the same record boundary, never from its middle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scopiengine.errors import IngestError
from scopiengine.ingest.reader import Line

__all__ = ["Record", "RecordAssembler"]


@dataclass(frozen=True, slots=True)
class Record:
    """One logical record — one or more folded physical lines.

    Attributes:
        text: The record body: single line text, or multiple lines joined with ``\\n``.
        offset: Byte offset of the record's first line — its identity for
            checkpoints and for ``--id-mode offset``.
        end_offset: Byte offset immediately past the record's last line. This
            is what a checkpoint advances to once the record is durably indexed.
        line_count: Number of physical lines folded into this record.
        truncated: Whether any folded line was truncated (see
            :class:`~scopiengine.ingest.reader.ChunkedByteReader`).
    """

    text: str
    offset: int
    end_offset: int
    line_count: int
    truncated: bool


class RecordAssembler:
    """Stateful line-to-record folder — feed lines in, get completed records out.

    Args:
        multiline_start: A regex; a line matching it (searched with
            ``re.match``, i.e. anchored at the start) begins a new record. When
            ``None``, every line is its own record and no state is held across
            calls.

    Raises:
        IngestError: ``multiline_start`` does not compile as a regex.
    """

    def __init__(self, *, multiline_start: str | None = None) -> None:
        self._pattern: re.Pattern[str] | None = None
        if multiline_start is not None:
            try:
                self._pattern = re.compile(multiline_start)
            except re.error as exc:
                raise IngestError(f"invalid --multiline-start pattern: {exc}") from exc
        self._pending: list[Line] = []

    @property
    def has_pending(self) -> bool:
        """Whether a multiline group is currently open (unfinished, unreturned)."""
        return bool(self._pending)

    def feed(self, line: Line) -> Record | None:
        """Fold one physical line in.

        Returns:
            A completed :class:`Record` if this line's arrival finalised the
            previously pending group (or immediately, in single-line mode);
            ``None`` while a multiline group is still being accumulated.
        """
        if self._pattern is None:
            return _to_record([line])
        if not self._pending or self._pattern.match(line.text):
            completed = self._flush()
            self._pending = [line]
            return completed
        self._pending.append(line)
        return None

    def close(self) -> Record | None:
        """Flush a currently-open group as a final record.

        Call this only once the source is truly, permanently exhausted (a
        one-shot batch run's real EOF, or a rotated-out file drained to its
        end) — never on a follow-mode "nothing new right now" pause, which
        might still continue the same group.
        """
        return self._flush()

    def _flush(self) -> Record | None:
        if not self._pending:
            return None
        lines, self._pending = self._pending, []
        return _to_record(lines)


def _to_record(lines: list[Line]) -> Record:
    return Record(
        text="\n".join(line.text for line in lines),
        offset=lines[0].start,
        end_offset=lines[-1].end,
        line_count=len(lines),
        truncated=any(line.truncated for line in lines),
    )
