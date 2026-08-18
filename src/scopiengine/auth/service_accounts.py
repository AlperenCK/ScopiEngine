"""Local Service Account authentication for the web UI.

A Service Account is a username/password pair defined and stored by
ScopiEngine itself (:class:`~scopiengine.storage.models.UIAccount`) — the
counterpart, once it exists, to an AD/LDAP identity verified against an
external directory instead.
"""

from __future__ import annotations

from scopiengine.auth.passwords import verify_password
from scopiengine.errors import AuthenticationError
from scopiengine.storage.base import StorageBackend

__all__ = ["authenticate_service_account"]


def authenticate_service_account(backend: StorageBackend, username: str, password: str) -> str:
    """Verify a username/password pair against a locally-defined UI account.

    Returns the account's username, as stored, on success.

    Raises:
        AuthenticationError: The account does not exist, is disabled, or the
            password is wrong. Deliberately the same error in every case, so
            a failed login never reveals which part was incorrect.
    """
    account = backend.get_ui_account(username)
    if account is None or account.disabled or not verify_password(password, account.password_hash):
        raise AuthenticationError("invalid username or password")
    return account.username
