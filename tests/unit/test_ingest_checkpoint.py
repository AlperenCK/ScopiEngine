"""Checkpoint identity and the resume/rotation/truncation decision matrix - all pure functions."""

from __future__ import annotations

from scopiengine.ingest.checkpoint import (
    advance,
    classify_change,
    compute_cp_key,
    decide_resume,
    new_checkpoint,
    short_sig,
)


def test_cp_key_is_stable_and_deterministic() -> None:
    a = compute_cp_key("logs", "/var/log/app.log")
    b = compute_cp_key("logs", "/var/log/app.log")
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_cp_key_differs_by_index_or_path() -> None:
    base = compute_cp_key("logs", "/var/log/app.log")
    assert compute_cp_key("metrics", "/var/log/app.log") != base
    assert compute_cp_key("logs", "/var/log/other.log") != base


def test_short_sig_is_deterministic_and_sig_specific() -> None:
    assert short_sig("1:2:abc") == short_sig("1:2:abc")
    assert short_sig("1:2:abc") != short_sig("1:2:def")


def test_new_checkpoint_starts_running_with_zero_records() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="s", byte_offset=42)
    assert cp.state == "running"
    assert cp.byte_offset == 42
    assert cp.record_count == 0
    assert cp.error is None


def test_advance_accumulates_record_count_and_clears_error_by_default() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="s")
    cp = advance(cp, byte_offset=100, record_count_delta=5, error="boom")
    assert cp.byte_offset == 100
    assert cp.record_count == 5
    assert cp.error == "boom"
    cp = advance(cp, byte_offset=200, record_count_delta=5)
    assert cp.record_count == 10
    assert cp.error is None  # a healthy advance clears a stale error


def test_advance_keeps_sig_unless_a_new_one_is_given() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="sig-a")
    cp = advance(cp, byte_offset=1)
    assert cp.source_sig == "sig-a"
    cp = advance(cp, byte_offset=2, sig="sig-b")
    assert cp.source_sig == "sig-b"


# -- classify_change -----------------------------------------------------------


def test_classify_fresh_when_nothing_was_tracked() -> None:
    assert classify_change(None, 0, "sig", 100) == "fresh"


def test_classify_unchanged_when_sig_matches_and_size_grew() -> None:
    assert classify_change("sig", 500, "sig", 900) == "unchanged"


def test_classify_unchanged_when_size_exactly_matches_tracked_extent() -> None:
    assert classify_change("sig", 500, "sig", 500) == "unchanged"


def test_classify_rotated_when_sig_differs() -> None:
    assert classify_change("sig-a", 500, "sig-b", 10) == "rotated"


def test_classify_truncated_when_sig_matches_but_size_shrank() -> None:
    assert classify_change("sig", 500, "sig", 100) == "truncated"


# -- decide_resume ---------------------------------------------------------------


def test_from_start_always_wins_regardless_of_checkpoint() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="sig", byte_offset=999)
    decision = decide_resume(cp, current_sig="sig", current_size=2000, from_mode="start")
    assert decision.start_offset == 0
    assert decision.reason == "start"
    assert decision.is_restart


def test_from_end_seeks_to_current_size() -> None:
    decision = decide_resume(None, current_sig="sig", current_size=12345, from_mode="end")
    assert decision.start_offset == 12345
    assert decision.reason == "end"


def test_checkpoint_mode_with_no_prior_checkpoint_is_fresh() -> None:
    decision = decide_resume(None, current_sig="sig", current_size=100, from_mode="checkpoint")
    assert decision.start_offset == 0
    assert decision.reason == "fresh"
    assert decision.is_restart


def test_checkpoint_mode_resumes_when_sig_matches_and_size_is_sufficient() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="sig", byte_offset=500)
    decision = decide_resume(cp, current_sig="sig", current_size=900, from_mode="checkpoint")
    assert decision.start_offset == 500
    assert decision.reason == "resume"
    assert not decision.is_restart
    assert decision.sig == "sig"


def test_checkpoint_mode_restarts_on_truncation_same_sig_smaller_size() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="sig", byte_offset=5000)
    decision = decide_resume(cp, current_sig="sig", current_size=100, from_mode="checkpoint")
    assert decision.start_offset == 0
    assert decision.reason == "restart_truncated"
    assert decision.is_restart


def test_checkpoint_mode_restarts_on_rotation_different_sig() -> None:
    cp = new_checkpoint(
        cp_key="k", index="logs", source_uri="/x.log", sig="sig-old", byte_offset=5000
    )
    decision = decide_resume(cp, current_sig="sig-new", current_size=10, from_mode="checkpoint")
    assert decision.start_offset == 0
    assert decision.reason == "restart_rotated"
    assert decision.sig == "sig-new"
    assert decision.is_restart


def test_rotation_and_truncation_are_reported_with_distinct_reasons() -> None:
    cp = new_checkpoint(cp_key="k", index="logs", source_uri="/x.log", sig="sig", byte_offset=5000)
    rotated = decide_resume(cp, current_sig="other", current_size=10, from_mode="checkpoint")
    truncated = decide_resume(cp, current_sig="sig", current_size=10, from_mode="checkpoint")
    assert rotated.reason != truncated.reason
    assert {rotated.reason, truncated.reason} == {"restart_rotated", "restart_truncated"}
