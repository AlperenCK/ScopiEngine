"""Property tests for the order-preserving term encoding.

No ``hypothesis`` dependency is available in this environment, so the property
--- ``encode(a) < encode(b) <=> a < b`` --- is checked directly against a large,
seeded, reproducible sample of randomised values instead: every pair drawn from
each sample is compared both ways, which is exactly what a property-based test
would generate, just without the shrinking machinery.
"""

from __future__ import annotations

import itertools
import math
import random

import pytest

from scopiengine.errors import MappingError
from scopiengine.mapping.encoding import (
    decode_date,
    decode_double,
    decode_long,
    encode_date,
    encode_double,
    encode_long,
    parse_iso8601_millis,
)

#: Fixed seed: failures are reproducible, not a coin flip on CI.
_SEED = 20260817
_SAMPLE_SIZE = 400

_LONG_MIN = -(2**63)
_LONG_MAX = 2**63 - 1


def _random_longs(rng: random.Random, n: int) -> list[int]:
    values = [rng.randint(_LONG_MIN, _LONG_MAX) for _ in range(n)]
    values += [_LONG_MIN, _LONG_MAX, 0, -1, 1]
    return values


def _random_doubles(rng: random.Random, n: int) -> list[float]:
    values = [rng.uniform(-1e18, 1e18) for _ in range(n)]
    values += [rng.uniform(-1.0, 1.0) for _ in range(n // 4)]  # near-zero density
    values += [
        0.0,
        -0.0,
        1.0,
        -1.0,
        float("inf"),
        float("-inf"),
        5e-324,  # smallest positive subnormal
        -5e-324,
        1.7976931348623157e308,  # DBL_MAX
        -1.7976931348623157e308,
    ]
    return values


# -- long ----------------------------------------------------------------------


def test_encode_long_orders_like_int_over_a_random_sample() -> None:
    rng = random.Random(_SEED)
    values = _random_longs(rng, _SAMPLE_SIZE)
    for a, b in itertools.islice(itertools.product(values, repeat=2), 0, None):
        assert (encode_long(a) < encode_long(b)) == (a < b), (a, b)


def test_encode_long_round_trips() -> None:
    rng = random.Random(_SEED)
    for value in _random_longs(rng, _SAMPLE_SIZE):
        assert decode_long(encode_long(value)) == value


def test_encode_long_rejects_out_of_range_values() -> None:
    with pytest.raises(MappingError):
        encode_long(_LONG_MAX + 1)
    with pytest.raises(MappingError):
        encode_long(_LONG_MIN - 1)


def test_encode_long_produces_fixed_width_hex() -> None:
    for value in (0, -1, _LONG_MIN, _LONG_MAX):
        assert len(encode_long(value)) == 16


# -- double ----------------------------------------------------------------------


def test_encode_double_orders_like_float_over_a_random_sample() -> None:
    rng = random.Random(_SEED)
    values = _random_doubles(rng, _SAMPLE_SIZE)
    for a, b in itertools.product(values, repeat=2):
        assert (encode_double(a) < encode_double(b)) == (a < b), (a, b)


def test_encode_double_round_trips_finite_values() -> None:
    rng = random.Random(_SEED)
    for value in _random_doubles(rng, _SAMPLE_SIZE):
        if math.isnan(value):
            continue
        assert decode_double(encode_double(value)) == value


def test_encode_double_treats_signed_zero_as_equal() -> None:
    assert encode_double(0.0) == encode_double(-0.0)


def test_encode_double_orders_infinities_correctly() -> None:
    lo = encode_double(float("-inf"))
    mid = encode_double(0.0)
    hi = encode_double(float("inf"))
    assert lo < mid < hi


def test_encode_double_rejects_nan() -> None:
    with pytest.raises(MappingError):
        encode_double(float("nan"))


# -- date ------------------------------------------------------------------------


def test_encode_date_orders_chronologically() -> None:
    earlier = encode_date("2025-01-01T00:00:00Z")
    later = encode_date("2026-06-15T12:30:00Z")
    assert earlier < later


def test_encode_date_z_and_offset_are_equivalent() -> None:
    assert encode_date("2026-01-01T00:00:00Z") == encode_date("2026-01-01T00:00:00+00:00")


def test_encode_date_naive_timestamp_treated_as_utc() -> None:
    assert encode_date("2026-01-01T00:00:00") == encode_date("2026-01-01T00:00:00Z")


def test_encode_date_round_trips_to_millisecond_precision() -> None:
    original = "2026-03-04T05:06:07.890000+00:00"
    decoded = decode_date(encode_date(original))
    assert parse_iso8601_millis(decoded) == parse_iso8601_millis(original)


def test_encode_date_rejects_malformed_input() -> None:
    with pytest.raises(MappingError):
        encode_date("not a date")
