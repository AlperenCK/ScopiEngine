"""Peak memory has a hard ceiling independent of file size.

Marked ``slow`` (deselected by ``pytest -m "not slow"``, the CI default) because
it writes and ingests a real, sizeable file. The size here is chosen to be honest
about what it proves in a CI-friendly amount of time, not to stand in for a
production-scale (1 GB+) file — see ``docs/INGEST.md``'s "Bounded memory" section
for the larger-scale figure this was manually validated against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scopiengine.engine import Engine
from scopiengine.settings import Settings

#: The two file sizes compared. A process that materialises (or otherwise
#: scales memory with) the file it reads would show peak RSS growing by
#: roughly the size difference between them; a properly bounded pipeline's
#: peak stays essentially flat. Sized to be honest about what a CI-friendly
#: run can prove in well under a minute - see docs/INGEST.md's "Bounded
#: memory" section for the multi-gigabyte figure this was validated against
#: by hand. Full-text indexing dominates ingest throughput (not the pipeline
#: itself), so both files use short, structured fields via ``json_line``
#: rather than one long free-text field per line.
_SMALL_FILE_BYTES = 5 * 1024 * 1024
_LARGE_FILE_BYTES = 60 * 1024 * 1024

#: Peak RSS ceiling for the ingest subprocess, at either size. Generous on
#: purpose - this is a boundedness proof, not a tight benchmark; see
#: docs/INGEST.md for the tuning knobs (``queue_size``, ``buffer_mb``,
#: ``batch_bytes``) that trade this ceiling against throughput.
_RSS_CEILING_KB = 350_000

#: How much peak RSS is allowed to grow between the small and large runs.
#: A file-size-proportional implementation would blow well past this for an
#: 11x size increase; a bounded one is dominated by fixed interpreter/driver
#: overhead and barely moves.
_MAX_RSS_GROWTH_KB = 60_000


def _write_large_log_file(path: Path, target_bytes: int) -> int:
    row = (
        json.dumps(
            {
                "seq": 0,
                "level": "INFO",
                "service": "auth",
                "msg": "some realistic padding text 0123456789",
            }
        )
        + "\n"
    ).encode()
    block = row * 5000
    written = 0
    with path.open("wb") as fh:
        while written < target_bytes:
            fh.write(block)
            written += len(block)
    return written


def _peak_rss_kb(proc: subprocess.Popen[bytes]) -> int:
    """Poll ``/proc/<pid>/status`` for ``VmHWM`` (the kernel's own running peak)."""
    status_path = Path(f"/proc/{proc.pid}/status")
    peak = 0
    while proc.poll() is None:
        try:
            text = status_path.read_text()
        except OSError:
            break
        for line in text.splitlines():
            if line.startswith("VmHWM:"):
                peak = max(peak, int(line.split()[1]))
                break
        time.sleep(0.02)
    return peak


def _run_ingest_and_measure_peak_rss(tmp_path: Path, name: str, target_bytes: int) -> int:
    log_path = tmp_path / f"{name}.log"
    _write_large_log_file(log_path, target_bytes)

    dsn = f"sqlite:///{tmp_path / f'{name}.db'}"
    with Engine.open(Settings(storage=dsn)) as engine:
        engine.create_index("logs")

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SCOPI_"):
            del env[key]

    proc = subprocess.Popen(
        [
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
            "--no-progress",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    peak_kb = _peak_rss_kb(proc)
    _, stderr = proc.communicate(timeout=180)
    assert proc.returncode == 0, stderr.decode()
    assert peak_kb > 0, "could not sample child RSS - /proc unavailable?"
    return peak_kb


@pytest.mark.slow
def test_peak_memory_is_bounded_independent_of_file_size(tmp_path: Path) -> None:
    small_peak_kb = _run_ingest_and_measure_peak_rss(tmp_path, "small", _SMALL_FILE_BYTES)
    large_peak_kb = _run_ingest_and_measure_peak_rss(tmp_path, "large", _LARGE_FILE_BYTES)

    for label, peak_kb in (("small", small_peak_kb), ("large", large_peak_kb)):
        assert peak_kb < _RSS_CEILING_KB, (
            f"{label} run: peak RSS {peak_kb} KiB exceeded the ceiling"
        )

    growth_kb = large_peak_kb - small_peak_kb
    size_increase = _LARGE_FILE_BYTES / _SMALL_FILE_BYTES
    assert growth_kb < _MAX_RSS_GROWTH_KB, (
        f"peak RSS grew {growth_kb} KiB for a {size_increase:.0f}x larger file "
        f"(small={small_peak_kb} KiB, large={large_peak_kb} KiB) - memory should track "
        "queue/batch settings, not file size"
    )
