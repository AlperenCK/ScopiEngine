"""Service Account authentication: the account must exist, be enabled, and the
password must verify — every other case fails the same way, deliberately, so
a failed login never reveals which part was wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scopiengine.auth.passwords import hash_password
from scopiengine.auth.service_accounts import authenticate_service_account
from scopiengine.errors import AuthenticationError
from scopiengine.storage.models import UIAccount
from scopiengine.storage.sqlite_backend import SQLiteBackend


@pytest.fixture
def backend(tmp_path: Path) -> SQLiteBackend:
    b = SQLiteBackend(str(tmp_path / "scopi.db"))
    b.open()
    b.migrate()
    b.create_ui_account(
        UIAccount(
            username="alice",
            password_hash=hash_password("correct horse battery staple"),
            disabled=False,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
    return b


def test_correct_credentials_authenticate(backend: SQLiteBackend) -> None:
    assert authenticate_service_account(backend, "alice", "correct horse battery staple") == "alice"


def test_wrong_password_is_rejected(backend: SQLiteBackend) -> None:
    with pytest.raises(AuthenticationError):
        authenticate_service_account(backend, "alice", "wrong password")


def test_unknown_username_is_rejected(backend: SQLiteBackend) -> None:
    with pytest.raises(AuthenticationError):
        authenticate_service_account(backend, "bob", "correct horse battery staple")


def test_disabled_account_is_rejected_even_with_the_right_password(
    backend: SQLiteBackend,
) -> None:
    backend.set_ui_account_disabled("alice", True)
    with pytest.raises(AuthenticationError):
        authenticate_service_account(backend, "alice", "correct horse battery staple")


def test_wrong_password_and_unknown_username_raise_the_same_error(
    backend: SQLiteBackend,
) -> None:
    """A failed login must not leak which half was wrong."""
    with pytest.raises(AuthenticationError) as wrong_password:
        authenticate_service_account(backend, "alice", "wrong password")
    with pytest.raises(AuthenticationError) as unknown_user:
        authenticate_service_account(backend, "nobody", "irrelevant")
    assert str(wrong_password.value) == str(unknown_user.value)
