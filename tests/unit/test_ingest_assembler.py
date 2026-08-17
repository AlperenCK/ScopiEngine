"""``RecordAssembler``: single-line passthrough, multiline folding, incomplete-tail safety."""

from __future__ import annotations

import pytest

from scopiengine.errors import IngestError
from scopiengine.ingest.assembler import RecordAssembler
from scopiengine.ingest.reader import Line


def _line(text: str, start: int, end: int, *, truncated: bool = False) -> Line:
    return Line(text=text, start=start, end=end, truncated=truncated)


def test_single_line_mode_emits_each_line_immediately() -> None:
    assembler = RecordAssembler()
    r1 = assembler.feed(_line("one", 0, 4))
    r2 = assembler.feed(_line("two", 4, 8))
    assert r1 is not None
    assert r1.text == "one"
    assert r1.offset == 0
    assert r1.end_offset == 4
    assert r1.line_count == 1
    assert r2 is not None
    assert r2.text == "two"
    assert not assembler.has_pending


def test_single_line_mode_close_is_a_no_op() -> None:
    assembler = RecordAssembler()
    assembler.feed(_line("one", 0, 4))
    assert assembler.close() is None


def test_multiline_folds_continuation_lines_into_the_previous_record() -> None:
    assembler = RecordAssembler(multiline_start=r"^\d{4}-\d{2}-\d{2}")
    assert assembler.feed(_line("2024-01-01 ERROR boom", 0, 22)) is None
    assert assembler.feed(_line("  at foo.bar()", 22, 37)) is None
    assert assembler.feed(_line("  at baz.qux()", 37, 52)) is None
    completed = assembler.feed(_line("2024-01-02 INFO ok", 52, 71))
    assert completed is not None
    assert completed.text == "2024-01-01 ERROR boom\n  at foo.bar()\n  at baz.qux()"
    assert completed.offset == 0
    assert completed.end_offset == 52
    assert completed.line_count == 3


def test_first_line_always_starts_a_group_even_without_matching_the_pattern() -> None:
    assembler = RecordAssembler(multiline_start=r"^\d{4}-")
    # A continuation-shaped line arriving with nothing pending must still
    # start a group - otherwise the very first record could never complete.
    assert assembler.feed(_line("  stray continuation", 0, 21)) is None
    completed = assembler.feed(_line("2024-01-02 INFO ok", 21, 40))
    assert completed is not None
    assert completed.text == "  stray continuation"


def test_incomplete_trailing_record_is_never_returned_without_close() -> None:
    assembler = RecordAssembler(multiline_start=r"^\d{4}-")
    assembler.feed(_line("2024-01-01 ERROR boom", 0, 22))
    result = assembler.feed(_line("  at foo.bar()", 22, 37))
    assert result is None  # still folding, not a new record
    assert assembler.has_pending
    # Simulating "the process stopped here" (crash, follow-mode pause, SIGINT):
    # nothing further is fed, and close() is deliberately not called - the
    # group must simply never surface as a record.


def test_close_flushes_the_trailing_group_once_the_source_is_truly_done() -> None:
    assembler = RecordAssembler(multiline_start=r"^\d{4}-")
    assembler.feed(_line("2024-01-01 ERROR boom", 0, 22))
    assembler.feed(_line("  at foo.bar()", 22, 37))
    final = assembler.close()
    assert final is not None
    assert final.text == "2024-01-01 ERROR boom\n  at foo.bar()"
    assert final.end_offset == 37
    assert assembler.close() is None  # nothing left to flush a second time


def test_a_crash_mid_stack_trace_resumes_at_the_last_record_boundary() -> None:
    """The scenario the module docstring promises: a still-open multiline group
    is simply never handed back, so a resume re-reads it from its start line,
    never from the middle of the stack trace.
    """
    assembler = RecordAssembler(multiline_start=r"^\d{4}-")
    completed_offsets: list[int] = []
    for line in [
        _line("2024-01-01 INFO start", 0, 22),
        _line("2024-01-01 ERROR boom", 22, 44),
        _line("  at foo.bar()", 44, 59),
        _line("  at baz.qux()", 59, 74),
        # process "crashes" here - no more lines fed, close() never called
    ]:
        record = assembler.feed(line)
        if record is not None:
            completed_offsets.append(record.end_offset)
    # Only the first record (a single line, immediately terminated by the
    # second start-line) ever completed; the still-open ERROR group is held.
    assert completed_offsets == [22]
    assert assembler.has_pending


def test_truncated_flag_propagates_from_any_folded_line() -> None:
    assembler = RecordAssembler(multiline_start=r"^\d{4}-")
    assembler.feed(_line("2024-01-01 ERROR boom", 0, 22))
    assembler.feed(_line("x" * 50, 22, 73, truncated=True))
    completed = assembler.feed(_line("2024-01-02 INFO ok", 73, 92))
    assert completed is not None
    assert completed.truncated is True


def test_invalid_multiline_pattern_raises_ingest_error() -> None:
    with pytest.raises(IngestError):
        RecordAssembler(multiline_start="[unterminated")
