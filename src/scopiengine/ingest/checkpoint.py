"""Checkpoint identity, resume decisions, and the rotation/truncation classifier.

Every function here is pure — no filesystem, no storage backend — so the resume
matrix (the four bullets in the module docstring of
:mod:`scopiengine.ingest.pipeline`) is unit-testable without touching a real file
or database. :mod:`scopiengine.ingest.pipeline` is the only caller that wires
these decisions to an actual :class:`~scopiengine.ingest.sources.Source` and
:class:`~scopiengine.storage.base.StorageBackend`.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from scopiengine.storage.models import Checkpoint

__all__ = [
    "FromMode",
    "ResumeDecision",
    "advance",
    "classify_change",
    "compute_cp_key",
    "decide_resume",
    "new_checkpoint",
    "short_sig",
]

FromMode = Literal["start", "end", "checkpoint"]

#: Reasons a resume decision can carry — distinct strings so a warning/log line
#: never has to guess why a run restarted at zero.
_RESTART_ROTATED = "restart_rotated"
_RESTART_TRUNCATED = "restart_truncated"


def compute_cp_key(index: str, source_uri: str) -> str:
    """``blake2b(index|source_uri).hexdigest()[:16]`` — the checkpoint's stable key."""
    payload = f"{index}|{source_uri}".encode()
    return hashlib.blake2b(payload).hexdigest()[:16]


def short_sig(sig: str) -> str:
    """A compact, stable stand-in for a full source signature, used in ``--id-mode offset``.

    Deterministic per signature (same file version -> same short form, so a
    replayed run overwrites the same ids) and changes whenever ``sig`` does
    (rotation/truncation -> a fresh id space, so a stale offset from the old
    file version can never collide with the new one).
    """
    return hashlib.blake2b(sig.encode(), digest_size=6).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_checkpoint(
    *, cp_key: str, index: str, source_uri: str, sig: str, byte_offset: int = 0
) -> Checkpoint:
    """Build a fresh, running checkpoint starting at ``byte_offset``."""
    now = _now_iso()
    return Checkpoint(
        cp_key=cp_key,
        index_name=index,
        source_uri=source_uri,
        source_sig=sig,
        byte_offset=byte_offset,
        record_count=0,
        state="running",
        error=None,
        started_at=now,
        updated_at=now,
    )


def advance(
    checkpoint: Checkpoint,
    *,
    byte_offset: int,
    record_count_delta: int = 0,
    sig: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Checkpoint:
    """Return a copy of ``checkpoint`` advanced to ``byte_offset``, with a fresh timestamp."""
    return replace(
        checkpoint,
        byte_offset=byte_offset,
        record_count=checkpoint.record_count + record_count_delta,
        source_sig=sig if sig is not None else checkpoint.source_sig,
        state=state if state is not None else checkpoint.state,
        error=error,
        updated_at=_now_iso(),
    )


def classify_change(
    old_sig: str | None, tracked_extent: int, new_sig: str, new_size: int
) -> Literal["fresh", "rotated", "truncated", "unchanged"]:
    """Classify a source's current state against a previously observed one.

    Args:
        old_sig: Previously observed signature, or ``None`` when nothing has
            been observed yet (no checkpoint on record).
        tracked_extent: The byte position the previous observation was valid
            up to — a checkpoint's ``byte_offset`` at startup, or the running
            read offset during a follow-mode poll.
        new_sig: Freshly computed signature.
        new_size: Freshly observed size.

    Returns:
        ``"fresh"`` — nothing was being tracked yet.
        ``"rotated"`` — the signature changed: a different file now sits at
        this path (rename-then-create, or an outright replacement).
        ``"truncated"`` — same signature, but the file is now shorter than
        the tracked extent: truncated in place (a copytruncate rotation).
        ``"unchanged"`` — same file, same or more data than tracked.
    """
    if old_sig is None:
        return "fresh"
    if old_sig != new_sig:
        return "rotated"
    if new_size < tracked_extent:
        return "truncated"
    return "unchanged"


class ResumeDecision:
    """Where an ingestion run should start reading, and why.

    Attributes:
        start_offset: Absolute byte offset to seek to before reading.
        reason: One of ``"start"``, ``"end"``, ``"fresh"``, ``"resume"``,
            ``"restart_rotated"``, ``"restart_truncated"``.
        sig: The signature the new/continued checkpoint should carry.
    """

    __slots__ = ("reason", "sig", "start_offset")

    def __init__(self, start_offset: int, reason: str, sig: str) -> None:
        self.start_offset = start_offset
        self.reason = reason
        self.sig = sig

    @property
    def is_restart(self) -> bool:
        """Whether this decision discards any previously saved offset."""
        return self.reason in (_RESTART_ROTATED, _RESTART_TRUNCATED, "start", "fresh")

    def __repr__(self) -> str:
        return f"ResumeDecision(start_offset={self.start_offset}, reason={self.reason!r})"


def decide_resume(
    checkpoint: Checkpoint | None,
    *,
    current_sig: str,
    current_size: int,
    from_mode: FromMode,
) -> ResumeDecision:
    """Decide where to start reading, applying ``--from`` and then the checkpoint.

    ``from_mode`` always wins when it names an unconditional position
    (``"start"``/``"end"``); ``"checkpoint"`` (the default) falls through to
    comparing the saved checkpoint's signature and offset against the source's
    current state — see :func:`classify_change`.
    """
    if from_mode == "start":
        return ResumeDecision(0, "start", current_sig)
    if from_mode == "end":
        return ResumeDecision(current_size, "end", current_sig)

    if checkpoint is None:
        return ResumeDecision(0, "fresh", current_sig)

    change = classify_change(
        checkpoint.source_sig, checkpoint.byte_offset, current_sig, current_size
    )
    if change == "rotated":
        return ResumeDecision(0, _RESTART_ROTATED, current_sig)
    if change == "truncated":
        return ResumeDecision(0, _RESTART_TRUNCATED, current_sig)
    return ResumeDecision(checkpoint.byte_offset, "resume", current_sig)
