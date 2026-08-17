"""Field types a mapping can declare, and how each one turns a raw JSON value
into the term(s) the index actually stores.

``object`` is not indexed directly — :mod:`scopiengine.mapping.mapping` flattens
an object field's ``properties`` into dotted paths (``"user.name"``), so by the
time a value reaches :func:`terms_for_value` its field is always one of the
leaf, indexable types.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scopiengine.errors import MappingError
from scopiengine.mapping.encoding import encode_date, encode_double, encode_long

__all__ = [
    "FIELD_TYPES",
    "INDEXABLE_TYPES",
    "terms_for_value",
]

#: Every field type a mapping may declare, including ``object`` (structural only).
FIELD_TYPES: frozenset[str] = frozenset(
    {"text", "keyword", "long", "double", "boolean", "date", "object"}
)

#: Types that actually reach the inverted index — everything but ``object``.
INDEXABLE_TYPES: frozenset[str] = FIELD_TYPES - {"object"}

#: Default ``ignore_above`` for a dynamically generated ``.keyword`` sub-field —
#: values longer than this are still stored but not indexed as a keyword term.
DEFAULT_IGNORE_ABOVE = 256


def _scalar_term(field_type: str, value: Any, *, ignore_above: int | None) -> str | None:
    """Encode one scalar value as the term text for ``field_type``.

    Returns:
        The term, or ``None`` when the value should not be indexed at all
        (a keyword value longer than ``ignore_above``).

    Raises:
        MappingError: The value's Python type does not fit ``field_type``.
    """
    if field_type == "keyword":
        if not isinstance(value, str):
            raise MappingError(f"keyword field requires a string, got {type(value).__name__}")
        if ignore_above is not None and len(value) > ignore_above:
            return None
        return value
    if field_type == "long":
        if isinstance(value, bool) or not isinstance(value, int):
            raise MappingError(f"long field requires an integer, got {type(value).__name__}")
        return encode_long(value)
    if field_type == "double":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise MappingError(f"double field requires a number, got {type(value).__name__}")
        return encode_double(float(value))
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise MappingError(f"boolean field requires a bool, got {type(value).__name__}")
        return "true" if value else "false"
    if field_type == "date":
        if not isinstance(value, str):
            got = type(value).__name__
            raise MappingError(f"date field requires an ISO-8601 string, got {got}")
        return encode_date(value)
    raise MappingError(f"{field_type!r} has no scalar term encoding (use terms_for_value for text)")


def terms_for_value(
    field_type: str, value: Any, *, ignore_above: int | None = None
) -> Sequence[str]:
    """Turn one field's raw JSON value into the exact-match term(s) to index.

    Text fields are handled by :mod:`scopiengine.analysis` instead — analysis
    needs an :class:`~scopiengine.analysis.analyzer.Analyzer`, which this
    function has no access to, so callers must special-case ``"text"`` before
    reaching here.

    Args:
        field_type: One of :data:`INDEXABLE_TYPES`, excluding ``"text"``.
        value: The raw value, or a list of raw values (multi-valued field).
        ignore_above: For ``keyword`` fields, the longest value length still
            indexed as an exact-match term.

    Returns:
        Zero or more terms. Empty for ``None``, an empty list, or (for
        ``keyword``) a value longer than ``ignore_above``.

    Raises:
        MappingError: ``field_type`` is ``"text"`` or ``"object"``, or a value's
            Python type does not fit the declared type.
    """
    if field_type == "text":
        raise MappingError("text fields are analyzed, not encoded as exact-match terms")
    if field_type == "object":
        raise MappingError("object fields have no terms of their own; index their leaf fields")
    if field_type not in INDEXABLE_TYPES:
        raise MappingError(f"unknown field type: {field_type!r}")

    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    terms = []
    for item in values:
        if item is None:
            continue
        term = _scalar_term(field_type, item, ignore_above=ignore_above)
        if term is not None:
            terms.append(term)
    return terms
