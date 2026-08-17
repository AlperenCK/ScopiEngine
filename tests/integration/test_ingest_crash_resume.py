"""The headline guarantee: a real ``SIGKILL`` mid-run, then ``--resume``, matches an
uninterrupted run exactly - same document count, same id set, no duplicates, no gaps.

This genuinely kills a child process (not a simulated interruption), which is the
only way to prove the single-transaction guarantee in
:mod:`scopiengine.ingest.pipeline` is structural rather than merely intended: a
crash that lands anywhere except exactly between two flushed batches must leave
the backend in one of only two states, "before this batch" or "after it" -
never in between.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from scopiengine.engine import Engine
from scopiengine.settings import Settings
from scopiengine.storage.factory import open_storage

_LINE_COUNT = 60_000
_BATCH_SIZE = 250


def _write_log_file(path: Path, n: int) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(
                json.dumps(
                    {
                        "seq": i,
                        "level": "ERROR" if i % 37 == 0 else "INFO",
                        "msg": f"event number {i} - a bit of padding text for realism",
                    }
                )
            )
            fh.write("\n")


def _create_index(dsn: str, name: str = "logs") -> None:
    with Engine.open(Settings(storage=dsn)) as engine:
        engine.create_index(name)


def _live_doc_ids(dsn: str, index: str = "logs") -> set[str]:
    with open_storage(dsn) as backend:
        info = backend.get_index(index)
        deleted = set(backend.iter_deleted(index))
        wanted = [o for o in range(info.next_ord) if o not in deleted]
        ids: set[str] = set()
        for start in range(0, len(wanted), 1000):
            chunk = wanted[start : start + 1000]
            for doc in backend.get_documents(index, chunk):
                ids.add(doc.doc_id)
        return ids


def _run_ingest(
    dsn: str, log_path: Path, *, extra_args: list[str] | None = None
) -> subprocess.Popen[bytes]:
    args = [
        sys.executable,
        "-m",
        "scopiengine.cli.main",
        "--storage",
        dsn,
        "ingest",
        "file",
        str(log_path),
        "--index",
        "logs",
        "--processor",
        "json_line",
        "--batch-size",
        str(_BATCH_SIZE),
        "--no-progress",
        *(extra_args or []),
    ]
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SCOPI_"):
            del env[key]
    return subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _checkpoint_offset(dsn: str) -> int | None:
    with open_storage(dsn) as backend:
        checkpoints = backend.list_checkpoints("logs")
        return checkpoints[0].byte_offset if checkpoints else None


def test_sigkill_mid_run_then_resume_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    log_path = tmp_path / "huge.log"
    _write_log_file(log_path, _LINE_COUNT)
    file_size = log_path.stat().st_size

    # -- reference: one uninterrupted run into its own database -------------
    dsn_reference = f"sqlite:///{tmp_path / 'reference.db'}"
    _create_index(dsn_reference)
    reference_proc = _run_ingest(dsn_reference, log_path)
    _, stderr = reference_proc.communicate(timeout=120)
    assert reference_proc.returncode == 0, stderr.decode()
    reference_ids = _live_doc_ids(dsn_reference)
    assert len(reference_ids) == _LINE_COUNT

    # -- the actual drill: kill mid-run, then resume -------------------------
    dsn_killed = f"sqlite:///{tmp_path / 'killed.db'}"
    _create_index(dsn_killed)
    proc = _run_ingest(dsn_killed, log_path)

    target_offset = file_size // 3
    deadline = time.monotonic() + 60
    killed_with_progress = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break  # finished before we got a chance to kill it - rare, handled below
        offset = _checkpoint_offset(dsn_killed)
        if offset is not None and offset >= target_offset:
            killed_with_progress = True
            break
        time.sleep(0.02)
    proc.kill()  # SIGKILL on POSIX
    proc.wait(timeout=30)

    assert killed_with_progress, (
        "ingest finished before it could be killed mid-run; rerun with a larger file"
    )
    offset_at_kill = _checkpoint_offset(dsn_killed)
    assert offset_at_kill is not None
    assert 0 < offset_at_kill < file_size

    resume_proc = _run_ingest(dsn_killed, log_path, extra_args=["--resume"])
    _, resume_stderr = resume_proc.communicate(timeout=120)
    assert resume_proc.returncode == 0, resume_stderr.decode()

    resumed_ids = _live_doc_ids(dsn_killed)
    assert resumed_ids == reference_ids
    assert len(resumed_ids) == _LINE_COUNT

    with open_storage(dsn_killed) as backend:
        final_checkpoint = backend.list_checkpoints("logs")[0]
        assert final_checkpoint.byte_offset == file_size
        assert final_checkpoint.state == "done"
        assert final_checkpoint.record_count == _LINE_COUNT
