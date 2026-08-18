"""``compute_signature`` must be stable across ordinary appends and still catch
genuine rotation — the property the whole follow-mode rotation story rests on.
"""

from __future__ import annotations

from pathlib import Path

from scopiengine.ingest.sources.local import SIGNATURE_HEAD_BYTES, compute_signature


def test_signature_is_stable_across_appends_below_the_head_window(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("line0\n")
    assert path.stat().st_size < SIGNATURE_HEAD_BYTES

    first = compute_signature(path, size=path.stat().st_size)
    for i in range(1, 6):
        with path.open("a") as fh:
            fh.write(f"line{i}\n")
        assert path.stat().st_size < SIGNATURE_HEAD_BYTES
        again = compute_signature(path, size=path.stat().st_size)
        assert again == first, f"signature changed on append {i} while still below the window"


def test_signature_is_stable_once_past_the_head_window_and_appended_further(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_bytes(b"x" * (SIGNATURE_HEAD_BYTES + 500) + b"\n")

    first = compute_signature(path, size=path.stat().st_size)
    with path.open("ab") as fh:
        fh.write(b"more data appended after the head window\n")
    again = compute_signature(path, size=path.stat().st_size)
    assert again == first


def test_signature_changes_when_content_crosses_the_head_window(tmp_path: Path) -> None:
    """Below the window, growth is invisible to the content hash by design; once the
    file has grown past it, the hash reflects the now-fixed first window and further
    growth stops moving it — the transition itself is allowed to change the
    signature exactly once.
    """
    path = tmp_path / "app.log"
    path.write_text("short\n")
    below = compute_signature(path, size=path.stat().st_size)

    with path.open("a") as fh:
        fh.write("x" * (SIGNATURE_HEAD_BYTES + 100))
    above = compute_signature(path, size=path.stat().st_size)
    assert above != below


def test_signature_differs_for_a_genuinely_different_small_file(tmp_path: Path) -> None:
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("line0\n")
    b.write_text("line0\n")

    sig_a = compute_signature(a, size=a.stat().st_size)
    sig_b = compute_signature(b, size=b.stat().st_size)
    assert sig_a != sig_b, "different inodes must yield different signatures"


def test_signature_differs_for_a_genuinely_different_large_file(tmp_path: Path) -> None:
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_bytes(b"a" * (SIGNATURE_HEAD_BYTES + 10))
    b.write_bytes(b"b" * (SIGNATURE_HEAD_BYTES + 10))

    sig_a = compute_signature(a, size=a.stat().st_size)
    sig_b = compute_signature(b, size=b.stat().st_size)
    assert sig_a != sig_b
