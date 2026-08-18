"""Session issuance and verification for the web UI login gate.

A session's identity lives entirely in the storage backend (:class:`UISession`
plus :meth:`StorageBackend.create_ui_session`/``get_ui_session``/
``delete_ui_session``) — this module only generates the random token, hashes
it the same way a password is hashed (never stored raw), and applies the
expiry policy.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from scopiengine.storage.base import StorageBackend
from scopiengine.storage.models import UISession

__all__ = [
    "SESSION_COOKIE_NAME",
    "create_session",
    "hash_token",
    "resolve_session",
    "revoke_session",
]

#: Name of the cookie carrying the raw session token.
SESSION_COOKIE_NAME = "scopi_ui_session"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_token(raw_token: str) -> str:
    """Hash a raw session token the way it is stored — never the token itself."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(
    backend: StorageBackend, *, principal: str, auth_method: str, ttl_seconds: int
) -> str:
    """Issue a new session for ``principal`` and return the raw token.

    The raw token is returned exactly once, here — only its hash is ever
    persisted, so losing the storage backend's contents does not hand out
    working session tokens.
    """
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    backend.create_ui_session(
        UISession(
            session_id_hash=hash_token(raw_token),
            principal=principal,
            auth_method=auth_method,
            created_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        )
    )
    return raw_token


def resolve_session(backend: StorageBackend, raw_token: str) -> UISession | None:
    """Look up a session by its raw token, or ``None`` if absent or expired.

    An expired session is deleted as a side effect of being resolved — no
    separate garbage-collection pass has to run for it to stop working the
    moment it lapses. (:meth:`StorageBackend.delete_expired_ui_sessions` still
    exists for periodic cleanup of sessions nobody ever presents again.)
    """
    session = backend.get_ui_session(hash_token(raw_token))
    if session is None:
        return None
    if session.expires_at <= _now_iso():
        backend.delete_ui_session(session.session_id_hash)
        return None
    return session


def revoke_session(backend: StorageBackend, raw_token: str) -> None:
    """Delete a session by its raw token. A no-op if it does not exist."""
    backend.delete_ui_session(hash_token(raw_token))
