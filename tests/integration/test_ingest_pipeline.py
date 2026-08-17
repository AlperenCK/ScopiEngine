"""``IngestPipeline`` end to end: idempotent resume, follow mode, rotation,
truncation, and a clean ``stop_event`` stop (the mechanism SIGINT/SIGTERM sit on
top of — see ``tests/integration/test_ingest_cli.py`` for the real-signal version).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from scopiengine.engine import Engine
from scopiengine.ingest.pipeline import IngestOptions, IngestPipeline
from scopiengine.ingest.sources.local import LocalFileSource
from scopiengine.settings import Settings


def _wait_until(predicate: Any, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition was never satisfied within the timeout"


def _open_engine(dsn: str, **overrides: Any) -> Engine:
    return Engine.open(Settings(storage=dsn, **overrides))


# -- basic run and idempotent replay ----------------------------------------


def test_basic_run_ingests_every_line_as_one_document(tmp_path: Path, storage_dsn: str) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")

    with _open_engine(storage_dsn, batch_size=10) as engine:
        engine.create_index("logs")
        pipeline = IngestPipeline(engine, LocalFileSource(log_path), IngestOptions(index="logs"))
        stats = pipeline.run()
        assert stats.state == "done"
        assert stats.docs_indexed == 50
        assert stats.records_read == 50
        assert engine.stats("logs")["doc_count"] == 50


def test_default_from_start_replay_is_a_no_op_under_offset_id_mode(
    tmp_path: Path, storage_dsn: str
) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")

    with _open_engine(storage_dsn, batch_size=10) as engine:
        engine.create_index("logs")
        options = IngestOptions(index="logs")
        IngestPipeline(engine, LocalFileSource(log_path), options).run()
        stats_before = engine.stats("logs")

        # Re-running from the start (the default, no --resume) with the same
        # file re-derives the same ids, so this must overwrite, not duplicate.
        IngestPipeline(engine, LocalFileSource(log_path), options).run()
        stats_after = engine.stats("logs")

        assert stats_after["live_doc_count"] == stats_before["live_doc_count"] == 50
        assert (
            stats_after["doc_count"] > stats_before["doc_count"]
        )  # old ordinals retired, not reused


def test_resume_continues_from_the_checkpoint_without_reprocessing(
    tmp_path: Path, storage_dsn: str
) -> None:
    log_path = tmp_path / "app.log"
    # Padded past the signature's 4 KiB head window: otherwise appending would
    # change the hash of the *whole* (still-under-4-KiB) file and this would
    # be exercising rotation detection, not a plain append-and-resume.
    log_path.write_text("\n".join(f"line {i} {'x' * 100}" for i in range(50)) + "\n")
    assert log_path.stat().st_size > 4096

    with _open_engine(storage_dsn, batch_size=10) as engine:
        engine.create_index("logs")
        options = IngestOptions(index="logs", from_mode="start")
        first = IngestPipeline(engine, LocalFileSource(log_path), options).run()
        assert first.docs_indexed == 50

        # Append more lines, then resume: only the new lines should be read.
        with log_path.open("a") as fh:
            fh.write("\n".join(f"line {i} {'x' * 100}" for i in range(50, 60)) + "\n")

        resume_options = IngestOptions(index="logs", from_mode="checkpoint")
        second = IngestPipeline(engine, LocalFileSource(log_path), resume_options).run()
        assert second.records_read == 10
        assert engine.stats("logs")["live_doc_count"] == 60


# -- follow mode --------------------------------------------------------------


def _run_in_background(
    pipeline: IngestPipeline, stop_event: threading.Event
) -> tuple[threading.Thread, dict]:
    result: dict[str, Any] = {}

    def _runner() -> None:
        result["stats"] = pipeline.run(stop_event=stop_event)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread, result


def test_follow_picks_up_appended_lines_within_flush_interval(
    tmp_path: Path, storage_dsn: str
) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text("line1\n")

    with _open_engine(storage_dsn, flush_interval=0.05, batch_size=1000, queue_size=4) as engine:
        engine.create_index("logs")
        options = IngestOptions(
            index="logs", follow=True, poll_min_interval=0.02, poll_max_interval=0.05
        )
        pipeline = IngestPipeline(engine, LocalFileSource(log_path), options)
        stop_event = threading.Event()
        thread, result = _run_in_background(pipeline, stop_event)
        try:
            _wait_until(lambda: engine.stats("logs")["doc_count"] >= 1)

            with log_path.open("a") as fh:
                fh.write("line2\n")

            _wait_until(lambda: engine.stats("logs")["doc_count"] >= 2, timeout=3.0)
        finally:
            stop_event.set()
            thread.join(timeout=5)
        assert result["stats"].state == "stopped"


# -- rotation and truncation, handled distinctly -----------------------------


def test_follow_handles_rotation_rename_then_new_file(tmp_path: Path, storage_dsn: str) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text("old1\nold2\n")

    with _open_engine(storage_dsn, flush_interval=0.05, batch_size=1000, queue_size=4) as engine:
        engine.create_index("logs")
        options = IngestOptions(
            index="logs", follow=True, poll_min_interval=0.02, poll_max_interval=0.05
        )
        pipeline = IngestPipeline(engine, LocalFileSource(log_path), options)
        stop_event = threading.Event()
        thread, result = _run_in_background(pipeline, stop_event)
        try:
            _wait_until(lambda: engine.stats("logs")["doc_count"] >= 2)
            original_sig = pipeline.storage.load_checkpoint(pipeline.cp_key).source_sig

            os.rename(log_path, tmp_path / "app.log.1")
            log_path.write_text("new1\n")

            _wait_until(lambda: engine.stats("logs")["doc_count"] >= 3, timeout=3.0)
            _wait_until(
                lambda: (
                    pipeline.storage.load_checkpoint(pipeline.cp_key).source_sig != original_sig
                ),
                timeout=3.0,
            )
        finally:
            stop_event.set()
            thread.join(timeout=5)
        assert result["stats"].state == "stopped"
        assert engine.stats("logs")["doc_count"] == 3
        checkpoint = pipeline.storage.load_checkpoint(pipeline.cp_key)
        assert checkpoint.source_sig != original_sig
        assert checkpoint.byte_offset == len(b"new1\n")


def test_follow_handles_truncation_in_place_distinctly_from_rotation(
    tmp_path: Path, storage_dsn: str
) -> None:
    log_path = tmp_path / "app.log"
    # A first "line" long enough that the signature's head-hash window (4 KiB)
    # sits entirely inside it, so a truncation that only trims content after
    # that point leaves the signature unchanged - the "same file, shorter"
    # shape a copytruncate rotation produces.
    first_line = "x" * 5000
    log_path.write_text(first_line + "\ntail\n")

    with _open_engine(storage_dsn, flush_interval=0.05, batch_size=1000, queue_size=4) as engine:
        engine.create_index("logs")
        options = IngestOptions(
            index="logs", follow=True, poll_min_interval=0.02, poll_max_interval=0.05
        )
        pipeline = IngestPipeline(engine, LocalFileSource(log_path), options)
        stop_event = threading.Event()
        thread, result = _run_in_background(pipeline, stop_event)
        try:
            _wait_until(lambda: engine.stats("logs")["doc_count"] >= 2)
            checkpoint = pipeline.storage.load_checkpoint(pipeline.cp_key)
            original_sig = checkpoint.source_sig
            offset_before_truncate = checkpoint.byte_offset
            assert offset_before_truncate > 4200

            os.truncate(log_path, 4200)
            # The truncation leaves a dangling partial line (byte 4200 lands
            # mid-way through the original 5000-byte first line); nothing
            # completes - and so nothing advances the checkpoint - until a
            # terminator arrives, exactly per the "never mid-record" rule.
            with log_path.open("a") as fh:
                fh.write("\nmore\n")

            _wait_until(
                lambda: (
                    pipeline.storage.load_checkpoint(pipeline.cp_key).byte_offset
                    < offset_before_truncate
                ),
                timeout=3.0,
            )
        finally:
            stop_event.set()
            thread.join(timeout=5)
        assert result["stats"].state == "stopped"
        checkpoint_after = pipeline.storage.load_checkpoint(pipeline.cp_key)
        # Same file (same signature) - this is what makes it "truncated",
        # not "rotated" - just shorter than we'd already read.
        assert checkpoint_after.source_sig == original_sig
        assert checkpoint_after.byte_offset == log_path.stat().st_size
        assert checkpoint_after.byte_offset < offset_before_truncate


# -- clean stop (what SIGINT/SIGTERM trigger) --------------------------------


def test_stop_event_drains_the_queue_flushes_and_leaves_a_resumable_checkpoint(
    tmp_path: Path, storage_dsn: str
) -> None:
    log_path = tmp_path / "app.log"
    lines = [json.dumps({"seq": i}) for i in range(2000)]
    log_path.write_text("\n".join(lines) + "\n")

    with _open_engine(storage_dsn, batch_size=25, flush_interval=60, queue_size=2) as engine:
        engine.create_index("logs")
        options = IngestOptions(index="logs", processors=("json_line",))
        pipeline = IngestPipeline(engine, LocalFileSource(log_path), options)
        stop_event = threading.Event()
        thread, result = _run_in_background(pipeline, stop_event)

        _wait_until(lambda: engine.stats("logs")["doc_count"] >= 25)
        stop_event.set()
        thread.join(timeout=10)

        stats = result["stats"]
        assert stats.state == "stopped"
        assert stats.checkpoint is not None
        assert 0 < stats.checkpoint.byte_offset < log_path.stat().st_size
        docs_after_stop = engine.stats("logs")["live_doc_count"]
        assert docs_after_stop == stats.docs_indexed

    # A fresh engine/process picks up exactly where the stop left off, and the
    # combined result matches an uninterrupted run - no duplicates, no gaps.
    with _open_engine(storage_dsn, batch_size=25, queue_size=2) as engine:
        resume_options = IngestOptions(
            index="logs", processors=("json_line",), from_mode="checkpoint"
        )
        resumed = IngestPipeline(engine, LocalFileSource(log_path), resume_options).run()
        assert resumed.state == "done"
        final_stats = engine.stats("logs")
        assert final_stats["live_doc_count"] == 2000
        assert docs_after_stop + resumed.docs_indexed == 2000
