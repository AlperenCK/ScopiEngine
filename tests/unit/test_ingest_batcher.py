"""``Batcher``: the three flush triggers, and offset advancement for dropped records."""

from __future__ import annotations

import time

from scopiengine.ingest.batcher import Batcher


def test_empty_batcher_never_flushes() -> None:
    batcher = Batcher(batch_size=10, batch_bytes=1_000_000, flush_interval=60)
    assert not batcher.should_flush
    assert batcher.take() is None


def test_batch_size_trigger() -> None:
    batcher = Batcher(batch_size=3, batch_bytes=1_000_000, flush_interval=60)
    for i in range(2):
        batcher.record_consumed(end_offset=i + 1)
        batcher.add_document({"i": i}, str(i), nbytes=1)
        assert not batcher.should_flush
    batcher.record_consumed(end_offset=3)
    batcher.add_document({"i": 2}, "2", nbytes=1)
    assert batcher.should_flush


def test_batch_bytes_trigger() -> None:
    batcher = Batcher(batch_size=1_000_000, batch_bytes=10, flush_interval=60)
    batcher.record_consumed(end_offset=1)
    batcher.add_document({}, "a", nbytes=5)
    assert not batcher.should_flush
    batcher.record_consumed(end_offset=2)
    batcher.add_document({}, "b", nbytes=6)
    assert batcher.should_flush


def test_flush_interval_trigger() -> None:
    batcher = Batcher(batch_size=1_000_000, batch_bytes=1_000_000, flush_interval=0.01)
    batcher.record_consumed(end_offset=1)
    batcher.add_document({}, "a", nbytes=1)
    assert not batcher.should_flush
    time.sleep(0.02)
    assert batcher.should_flush


def test_take_resets_state_and_deadline() -> None:
    batcher = Batcher(batch_size=2, batch_bytes=1_000_000, flush_interval=60)
    batcher.record_consumed(end_offset=1)
    batcher.add_document({}, "a", nbytes=1)
    batcher.record_consumed(end_offset=2)
    batcher.add_document({}, "b", nbytes=1)
    payload = batcher.take()
    assert payload is not None
    assert payload.docs == [{}, {}]
    assert payload.ids == ["a", "b"]
    assert payload.end_offset == 2
    assert payload.record_count == 2
    assert not batcher.should_flush
    assert batcher.take() is None


def test_dropped_records_still_advance_the_pending_offset() -> None:
    """A record with no kept document must still move the checkpoint forward -
    otherwise a resume would re-read (and re-drop) it forever, but it must not
    count toward the document-count/byte flush triggers.
    """
    batcher = Batcher(batch_size=1_000_000, batch_bytes=1_000_000, flush_interval=60)
    batcher.record_consumed(end_offset=10)  # dropped by a processor
    assert batcher.has_pending
    assert batcher.doc_count == 0
    payload = batcher.take()
    assert payload is not None
    assert payload.docs == []
    assert payload.end_offset == 10
    assert payload.record_count == 1


def test_has_pending_reflects_any_consumed_record() -> None:
    batcher = Batcher(batch_size=10, batch_bytes=1000, flush_interval=60)
    assert not batcher.has_pending
    batcher.record_consumed(end_offset=5)
    assert batcher.has_pending
    batcher.take()
    assert not batcher.has_pending


def test_time_remaining_is_floored_at_zero() -> None:
    batcher = Batcher(batch_size=10, batch_bytes=1000, flush_interval=0.01)
    time.sleep(0.02)
    assert batcher.time_remaining == 0.0
