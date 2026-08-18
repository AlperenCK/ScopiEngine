"""Password hashing for local UI accounts.

PBKDF2-HMAC-SHA256 via the standard library only — no new dependency for
something this ubiquitous. The algorithm, iteration count and salt travel
with the hash itself (:func:`hash_password`'s output), so raising the
default iteration count later never invalidates an already-stored hash.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = ["hash_password", "verify_password"]

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 260_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password for storage. Never store the raw password."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a password against a hash produced by :func:`hash_password`.

    Returns ``False`` — rather than raising — for a malformed stored hash, so
    a corrupted or foreign value never becomes a crash on the login path.
    """
    try:
        algorithm, iterations_str, salt_hex, digest_hex = stored_hash.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)
