"""Password hashing must round-trip correctly and never crash on bad input —
the login path calls :func:`verify_password` on whatever is in storage, and a
corrupted or foreign value there must fail closed, not raise.
"""

from __future__ import annotations

from scopiengine.auth.passwords import hash_password, verify_password


def test_hash_and_verify_round_trip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)


def test_verify_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_hash_is_salted_differently_each_time() -> None:
    first = hash_password("same password")
    second = hash_password("same password")
    assert first != second
    assert verify_password("same password", first)
    assert verify_password("same password", second)


def test_hash_never_stores_the_raw_password() -> None:
    assert "hunter2" not in hash_password("hunter2")


def test_verify_rejects_malformed_stored_hash_without_raising() -> None:
    assert not verify_password("anything", "not-a-real-hash")
    assert not verify_password("anything", "")
    assert not verify_password("anything", "pbkdf2_sha256$not-a-number$abcd$abcd")


def test_verify_rejects_hash_from_a_different_algorithm() -> None:
    foreign = "bcrypt$12$somesalt$somehash"
    assert not verify_password("anything", foreign)
