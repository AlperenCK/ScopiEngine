"""Session issuance, resolution and expiry — the mechanism the login gate
rests on. Every case here uses a real (SQLite) storage backend rather than a
mock: the whole point of :mod:`scopiengine.auth.sessions` is what it does to
storage, not just what it returns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scopiengine.auth.sessions import create_session, hash_token, resolve_session, revoke_session
from scopiengine.storage.models import UISession
from scopiengine.storage.sqlite_backend import SQLiteBackend


@pytest.fixture
def backend(tmp_path: Path) -> SQLiteBackend:
    b = SQLiteBackend(str(tmp_path / "scopi.db"))
    b.open()
    b.migrate()
    return b


def test_create_and_resolve_round_trip(backend: SQLiteBackend) -> None:
    token = create_session(
        backend, principal="alice", auth_method="service_account", ttl_seconds=3600
    )
    session = resolve_session(backend, token)
    assert session is not None
    assert session.principal == "alice"
    assert session.auth_method == "service_account"


def test_the_raw_token_is_never_the_stored_value(backend: SQLiteBackend) -> None:
    token = create_session(
        backend, principal="alice", auth_method="service_account", ttl_seconds=3600
    )
    stored = backend.get_ui_session(hash_token(token))
    assert stored is not None
    assert stored.session_id_hash != token


def test_resolve_rejects_an_unknown_token(backend: SQLiteBackend) -> None:
    assert resolve_session(backend, "this-token-was-never-issued") is None


def test_expired_session_is_rejected_and_cleaned_up(backend: SQLiteBackend) -> None:
    # Insert an already-expired session directly, bypassing create_session's
    # ttl_seconds-from-now math, so expiry is exercised deterministically.
    raw_token = "a-raw-token-for-this-test"
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    backend.create_ui_session(
        UISession(
            session_id_hash=hash_token(raw_token),
            principal="alice",
            auth_method="service_account",
            created_at=past,
            expires_at=past,
        )
    )
    assert resolve_session(backend, raw_token) is None
    # The lookup itself cleans up — the row must be gone, not just rejected.
    assert backend.get_ui_session(hash_token(raw_token)) is None


def test_revoke_invalidates_the_session(backend: SQLiteBackend) -> None:
    token = create_session(
        backend, principal="alice", auth_method="service_account", ttl_seconds=3600
    )
    assert resolve_session(backend, token) is not None
    revoke_session(backend, token)
    assert resolve_session(backend, token) is None


def test_revoke_of_an_unknown_token_is_a_no_op(backend: SQLiteBackend) -> None:
    revoke_session(backend, "never-issued")  # must not raise
