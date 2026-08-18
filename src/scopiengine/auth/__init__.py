"""Authentication for the web UI's login gate.

Scoped deliberately narrowly: this guards ``/_ui/`` only. The REST API itself
is unauthenticated, exactly as it was before this package existed — see
``docs/UI_AUTH.md`` for why, and for the ``SCOPI_UI_AUTH_ENABLED`` opt-in.
"""

from __future__ import annotations

from scopiengine.auth.passwords import hash_password, verify_password
from scopiengine.auth.service_accounts import authenticate_service_account
from scopiengine.auth.sessions import (
    SESSION_COOKIE_NAME,
    create_session,
    hash_token,
    resolve_session,
    revoke_session,
)

__all__ = [
    "SESSION_COOKIE_NAME",
    "authenticate_service_account",
    "create_session",
    "hash_password",
    "hash_token",
    "resolve_session",
    "revoke_session",
    "verify_password",
]
