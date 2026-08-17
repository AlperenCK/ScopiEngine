"""Order-preserving term encoding — the trick that lets numerics and dates share
one ``terms`` B-tree with text.

:meth:`~scopiengine.storage.base.StorageBackend.iter_terms` only knows how to range-
scan and prefix-scan *strings*. Rather than giving numeric and date fields a
second, bespoke storage path, every value is encoded to a fixed-width hex string
such that ordinary string comparison agrees with the value's natural ordering::

    encode(a) < encode(b)  <=>  a < b

for every representable ``a`` and ``b`` — negatives, zero, the extremes of the
range, and (for doubles) positive and negative infinity all included. That
invariant is what :func:`scopiengine.index.searcher` leans on for range queries
against numeric and date fields: a plain lexicographic ``iter_terms(lo=..., hi=...)``
scan does the right thing without ever branching on the field's declared type.

Encoding
--------
``long`` (64-bit signed integer)
    Shift into the unsigned range and hex-pack: ``format(v + 2**63, "016x")``.
    Shifting by the bias turns two's-complement ordering (where negative values
    have their top bit set and would otherwise sort *after* positives as raw
    integers) into a plain unsigned range, which sorts correctly as fixed-width
    hex text.

``double`` (IEEE-754 64-bit float)
    Reinterpret the 8 bytes as an unsigned 64-bit integer, then flip bits so
    that ordering the *bit pattern* as unsigned matches ordering the *float*:
    for a non-negative float, flip only the sign bit (so positives sort above
    the now sign-flipped negatives' encoding); for a negative float, flip every
    bit (so more-negative values, which have a larger raw magnitude, sort
    lower). This is the standard IEEE-754 "total order" bit-flip transform.
    Hex-pack the result the same way as ``long``. NaN has no defined position
    in this ordering, matching IEEE-754 itself (NaN is unordered) — callers
    must not encode NaN.

``date``
    Parsed from ISO-8601 to epoch milliseconds (UTC), then encoded via the
    ``long`` path — a date is simply an integer once you count milliseconds.
"""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime

from scopiengine.errors import MappingError

__all__ = [
    "decode_date",
    "decode_double",
    "decode_long",
    "encode_date",
    "encode_double",
    "encode_long",
    "parse_iso8601_millis",
]

#: Bias that maps the signed 64-bit range onto the unsigned one: ``-2**63`` becomes
#: ``0`` and ``2**63 - 1`` becomes ``2**64 - 1``.
_LONG_BIAS = 1 << 63
#: Mask for the low 64 bits, used when bit-flipping a double's representation.
_U64_MASK = (1 << 64) - 1
#: The sign bit alone, in the same 64-bit layout ``struct.pack('>d', ...)`` produces.
_SIGN_BIT = 1 << 63
#: Every encoded term is exactly this many hex digits — the fixed width is what
#: makes lexicographic string comparison agree with numeric comparison.
TERM_WIDTH = 16


def encode_long(value: int) -> str:
    """Encode a 64-bit signed integer as a 16-digit order-preserving hex string.

    Raises:
        MappingError: ``value`` does not fit in a signed 64-bit integer.
    """
    if not -(1 << 63) <= value < (1 << 63):
        raise MappingError(f"long value out of 64-bit signed range: {value}")
    return format(value + _LONG_BIAS, f"0{TERM_WIDTH}x")


def decode_long(term: str) -> int:
    """Invert :func:`encode_long`."""
    return int(term, 16) - _LONG_BIAS


def _double_bits(value: float) -> int:
    """Reinterpret a float's IEEE-754 bytes as an unsigned 64-bit integer."""
    if math.isnan(value):
        raise MappingError("cannot encode NaN: it has no defined ordering")
    if value == 0.0:
        # -0.0 and 0.0 compare equal, so they must encode identically — otherwise
        # encode(a) < encode(b) would disagree with a < b for that pair.
        value = 0.0
    (raw,) = struct.unpack(">Q", struct.pack(">d", value))
    return raw


def encode_double(value: float) -> str:
    """Encode an IEEE-754 double as a 16-digit order-preserving hex string.

    Raises:
        MappingError: ``value`` is NaN.
    """
    bits = _double_bits(value)
    if bits & _SIGN_BIT:  # noqa: SIM108 - the branch comments carry their own explanation
        # Negative: flip every bit, so a larger magnitude (more negative) yields
        # a smaller unsigned integer.
        ordered = ~bits & _U64_MASK
    else:
        # Non-negative: flip only the sign bit, so it sorts above every
        # (now sign-flipped) negative encoding.
        ordered = bits | _SIGN_BIT
    return format(ordered, f"0{TERM_WIDTH}x")


def decode_double(term: str) -> float:
    """Invert :func:`encode_double`."""
    ordered = int(term, 16)
    # Inverse of the two branches in encode_double: undo whichever transform
    # (sign-bit-only flip, or every-bit flip) produced this ordered value.
    bits = ordered & ~_SIGN_BIT if ordered & _SIGN_BIT else ~ordered & _U64_MASK
    (value,) = struct.unpack(">d", struct.pack(">Q", bits))
    return value


def parse_iso8601_millis(text: str) -> int:
    """Parse an ISO-8601 timestamp to epoch milliseconds (UTC).

    A timestamp with no timezone offset is treated as UTC.

    Raises:
        MappingError: ``text`` is not a valid ISO-8601 timestamp.
    """
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MappingError(f"not a valid ISO-8601 date: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = parsed - epoch
    return round(delta.total_seconds() * 1000)


def encode_date(text: str) -> str:
    """Parse an ISO-8601 string with :func:`parse_iso8601_millis`, then encode it as a ``long``."""
    return encode_long(parse_iso8601_millis(text))


def decode_date(term: str) -> str:
    """Invert :func:`encode_date`, returning an ISO-8601 UTC timestamp."""
    millis = decode_long(term)
    seconds, remainder_ms = divmod(millis, 1000)
    dt = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=remainder_ms * 1000)
    return dt.isoformat().replace("+00:00", "Z")
