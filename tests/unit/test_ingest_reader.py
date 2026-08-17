"""``ChunkedByteReader``: chunk-boundary correctness, truncation, CRLF, no trailing newline."""

from __future__ import annotations

import io
import time
from itertools import pairwise

import pytest

from scopiengine.ingest.reader import ChunkedByteReader


def _read_all(
    stream: io.BytesIO, *, chunk_bytes: int = 1024 * 1024, max_line_bytes: int = 1024 * 1024
):
    reader = ChunkedByteReader(stream, chunk_bytes=chunk_bytes, max_line_bytes=max_line_bytes)
    lines = list(reader.read_available())
    trailing = reader.close_final()
    if trailing is not None:
        lines.append(trailing)
    return lines, reader


def test_simple_lines_with_trailing_newline() -> None:
    data = b"one\ntwo\nthree\n"
    lines, reader = _read_all(io.BytesIO(data))
    assert [line.text for line in lines] == ["one", "two", "three"]
    assert reader.offset == len(data)


def test_no_trailing_newline_still_yields_final_line() -> None:
    data = b"one\ntwo\nthree"
    lines, reader = _read_all(io.BytesIO(data))
    assert [line.text for line in lines] == ["one", "two", "three"]
    assert reader.offset == len(data)


def test_crlf_line_endings_are_stripped() -> None:
    data = b"one\r\ntwo\r\nthree\r\n"
    lines, _reader = _read_all(io.BytesIO(data))
    assert [line.text for line in lines] == ["one", "two", "three"]


def test_offsets_are_absolute_and_contiguous() -> None:
    data = b"abc\nde\nfghij\n"
    lines, _reader = _read_all(io.BytesIO(data))
    spans = [(line.start, line.end) for line in lines]
    assert spans == [(0, 4), (4, 7), (7, 13)]
    # Every line's end is the next line's start - no gap, no overlap.
    for (_, end), (start, _) in pairwise(spans):
        assert end == start


def test_empty_lines_are_preserved() -> None:
    data = b"one\n\nthree\n"
    lines, _reader = _read_all(io.BytesIO(data))
    assert [line.text for line in lines] == ["one", "", "three"]


def test_multibyte_utf8_character_split_across_a_chunk_boundary_decodes_correctly() -> None:
    # A 3-byte character (E2 82 AC = the Euro sign) whose bytes straddle a
    # chunk boundary must still decode as one character, never as
    # replacement characters for the two fragment halves.
    text = "café " + "x" * 10 + "€ done"
    data = text.encode("utf-8") + b"\n"
    euro_byte_offset = data.index("€".encode())
    # Force the chunk boundary to land inside the euro sign's byte sequence.
    chunk_bytes = euro_byte_offset + 1
    lines, _reader = _read_all(io.BytesIO(data), chunk_bytes=chunk_bytes)
    assert len(lines) == 1
    assert lines[0].text == text
    assert "�" not in lines[0].text


def test_line_longer_than_max_line_bytes_is_truncated_not_unbounded() -> None:
    huge = b"x" * (5000) + b"\n" + b"short\n"
    lines, reader = _read_all(io.BytesIO(huge), max_line_bytes=100)
    assert len(lines[0].text) <= 100
    assert lines[0].truncated is True
    assert lines[1].text == "short"
    assert lines[1].truncated is False
    assert reader.truncated_line_count == 1
    # Offsets still account for every byte actually in the source, truncated or not.
    assert reader.offset == len(huge)


def test_truncated_line_reported_length_matches_the_cap_exactly() -> None:
    huge = (b"a" * 250) + b"\n"
    lines, _reader = _read_all(io.BytesIO(huge), max_line_bytes=64)
    assert len(lines[0].text) == 64


def test_over_long_line_spanning_multiple_chunks_still_bounds_memory() -> None:
    # The overflow must be dropped as it streams in, not accumulated across
    # many small reads before being capped.
    huge = (b"y" * 10_000) + b"\n"
    lines, reader = _read_all(io.BytesIO(huge), chunk_bytes=64, max_line_bytes=200)
    assert len(lines[0].text) <= 200
    assert lines[0].truncated is True
    assert reader.pending_bytes == 0


def test_read_available_stops_without_blocking_when_no_more_bytes_are_ready() -> None:
    stream = io.BytesIO(b"partial line without a newline yet")
    reader = ChunkedByteReader(stream)
    lines = list(reader.read_available())
    assert lines == []  # nothing complete yet
    assert reader.pending_bytes > 0


def test_close_final_flushes_a_pending_partial_line_once() -> None:
    stream = io.BytesIO(b"partial")
    reader = ChunkedByteReader(stream)
    list(reader.read_available())
    first = reader.close_final()
    assert first is not None
    assert first.text == "partial"
    assert reader.close_final() is None


def test_start_offset_carries_through_for_a_resumed_stream() -> None:
    data = b"tail-of-file\n"
    reader = ChunkedByteReader(io.BytesIO(data), start_offset=1_000_000)
    lines = list(reader.read_available())
    assert lines[0].start == 1_000_000
    assert lines[0].end == 1_000_000 + len(data)
    assert reader.offset == 1_000_000 + len(data)


class _FeedStream:
    """A stream that returns whatever has been ``feed``-ed so far, then ``b""``.

    Mimics tailing a file that is still being written to: a
    :meth:`~scopiengine.ingest.reader.ChunkedByteReader.read_available` call
    "catches up" and stops without blocking, exactly as it would against a
    real growing file.
    """

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, data: bytes) -> None:
        self._pending.extend(data)

    def read(self, size: int = -1) -> bytes:
        if not self._pending:
            return b""
        n = len(self._pending) if size is None or size < 0 else size
        chunk = bytes(self._pending[:n])
        del self._pending[:n]
        return chunk


def test_incremental_reads_across_separate_read_available_calls() -> None:
    stream = _FeedStream()
    reader = ChunkedByteReader(stream)

    stream.feed(b"first ")
    assert list(reader.read_available()) == []  # no newline yet

    stream.feed(b"half\nsecond")
    round2 = list(reader.read_available())
    assert [line.text for line in round2] == ["first half"]

    stream.feed(b" half\n")
    round3 = list(reader.read_available())
    assert [line.text for line in round3] == ["second half"]
    assert reader.offset == len(b"first half\nsecond half\n")


# -- progress accounting on a resumed run ----------------------------------------


def test_throughput_and_eta_measure_only_this_run() -> None:
    """A resumed run must not count the prefix it skipped as work it performed.

    ``bytes_read`` is an absolute file offset, so a run resuming at 900 MB starts
    with a large value and zero elapsed time. Dividing one by the other reports a
    throughput the process never achieved and an ETA far shorter than the work
    left, which is exactly the signal an operator watching a long ingest relies on.
    """
    from scopiengine.ingest.pipeline import IngestStats

    stats = IngestStats()
    stats.file_size = 1_000_000
    stats.start_offset = 900_000
    stats.bytes_read = 950_000
    stats._started = time.monotonic() - 10.0

    # 50_000 bytes in 10 seconds, not 950_000.
    assert stats.bytes_per_second == pytest.approx(5_000, rel=0.05)
    # 50_000 bytes remaining at that rate, not the near-zero a 95 KB/s rate implies.
    assert stats.eta_seconds == pytest.approx(10.0, rel=0.05)


def test_throughput_from_the_start_is_unaffected() -> None:
    from scopiengine.ingest.pipeline import IngestStats

    stats = IngestStats()
    stats.file_size = 1_000_000
    stats.bytes_read = 400_000
    stats._started = time.monotonic() - 4.0

    assert stats.bytes_per_second == pytest.approx(100_000, rel=0.05)
    assert stats.eta_seconds == pytest.approx(6.0, rel=0.05)
