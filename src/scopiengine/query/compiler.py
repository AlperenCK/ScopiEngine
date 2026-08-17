"""Shared field resolution: mapping lookup, analysis and encoding.

Both :mod:`scopiengine.query.scopiql` and :mod:`scopiengine.query.dsl` build the
exact same :mod:`scopiengine.query.ast` leaves from a ``(field, raw value)`` pair
through this one module, so ``level:ERROR`` and ``{"term": {"level": "ERROR"}}``
compile to byte-identical AST nodes — the guarantee the parity tests in
``tests/integration/test_api.py`` check directly.

Unmapped fields
----------------
A query naming a field the index has no mapping for is not an error: every leaf
AST node already degrades to "no results" for an unmapped field on its own — see
:func:`scopiengine.index.searcher._execute`, which looks the field up via
:meth:`~scopiengine.index.reader.IndexReader.field_id` and returns immediately
when it is ``None``, before ever touching the node's value. So this module never
needs to special-case "field not mapped" itself; it only has to special-case the
one situation the executor *can't* shrug off on its own — a value that, once
analyzed, carries zero terms (an all-stopword phrase, an empty string) — via
:data:`NO_MATCH`.
"""

from __future__ import annotations

from typing import Any

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import InvalidQueryError, MappingError, UnsupportedFeatureError
from scopiengine.mapping.encoding import encode_date, encode_double, encode_long
from scopiengine.mapping.fieldtypes import terms_for_value
from scopiengine.mapping.mapping import FieldMapping, Mapping
from scopiengine.query.ast import Bool, Exists, Phrase, Prefix, Query, Range, Term

__all__ = [
    "NO_MATCH",
    "analyzed_tokens",
    "coerce_scalar",
    "exists_query",
    "multi_field_phrase",
    "multi_field_term",
    "phrase_query",
    "prefix_query",
    "range_query",
    "term_query",
    "text_fields",
]

#: A query AST leaf guaranteed to match nothing, for any field, any mapping,
#: without ever touching storage: :class:`~scopiengine.query.ast.Phrase`
#: short-circuits on an empty ``terms`` tuple before it looks anything up.
#: Used wherever a value legitimately resolves to zero terms — an empty
#: string, or text that analyzes away entirely (e.g. every token is a stop word).
NO_MATCH: Query = Phrase(field="", terms=())


def analyzed_tokens(
    analyzers: AnalyzerRegistry, field_mapping: FieldMapping, text: str, *, search: bool = True
) -> list[str]:
    """Analyze ``text`` for a ``text`` field, returning just the term strings.

    Args:
        search: Use the field's *search*-time analyzer (falling back to its
            index-time one) when ``True``; use the index-time analyzer
            directly when ``False``. Query compilation always wants ``True``.
    """
    name = (field_mapping.effective_search_analyzer() if search else field_mapping.analyzer) or (
        "standard"
    )
    analyzer = analyzers.analyzer(name)
    return [term for term, _position in analyzer.analyze(text)]


def text_fields(mapping: Mapping) -> list[str]:
    """Every ``text``-typed field path in ``mapping``, sorted for a deterministic order."""
    return sorted(name for name, fm in mapping.fields.items() if fm.type == "text")


def coerce_scalar(field: str, field_mapping: FieldMapping, raw: Any) -> Any:
    """Coerce a raw query value (a ScopiQL token, always ``str``, or a JSON DSL
    scalar) to the Python type :func:`~scopiengine.mapping.fieldtypes.terms_for_value`
    expects for ``field_mapping.type``.

    Raises:
        InvalidQueryError: ``raw`` cannot be interpreted as that type.
    """
    field_type = field_mapping.type
    if field_type == "long":
        if not isinstance(raw, bool) and isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw.strip(), 10)
            except ValueError:
                pass
        raise InvalidQueryError(f"field {field!r} expects an integer, got {raw!r}")
    if field_type == "double":
        if not isinstance(raw, bool) and isinstance(raw, int | float):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw.strip())
            except ValueError:
                pass
        raise InvalidQueryError(f"field {field!r} expects a number, got {raw!r}")
    if field_type == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
        raise InvalidQueryError(f"field {field!r} expects a boolean, got {raw!r}")
    if field_type == "date":
        if isinstance(raw, str):
            return raw
        raise InvalidQueryError(f"field {field!r} expects an ISO-8601 date string, got {raw!r}")
    if field_type == "keyword":
        if isinstance(raw, str):
            return raw
        raise InvalidQueryError(f"field {field!r} expects a string, got {raw!r}")
    raise UnsupportedFeatureError(
        f"field {field!r} has type {field_type!r}, which has no query value"
    )


def _encoded_bound(field: str, field_mapping: FieldMapping, raw: Any) -> str:
    """Encode one :class:`~scopiengine.query.ast.Range` bound for ``field_mapping``.

    ``keyword`` compares its raw string lexicographically (already the storage
    order); ``long``/``double``/``date`` route through
    :mod:`scopiengine.mapping.encoding` so the bound agrees with the
    order-preserving terms an indexed value of that type produces.

    Raises:
        InvalidQueryError: ``raw`` cannot be coerced to ``field_mapping.type``,
            or (for ``date``) is not a valid ISO-8601 timestamp.
        UnsupportedFeatureError: ``field_mapping.type`` has no range ordering
            (``text``, ``boolean``, ``object``).
    """
    field_type = field_mapping.type
    if field_type == "keyword":
        value = coerce_scalar(field, field_mapping, raw)
        return str(value)
    if field_type == "long":
        return encode_long(coerce_scalar(field, field_mapping, raw))
    if field_type == "double":
        return encode_double(coerce_scalar(field, field_mapping, raw))
    if field_type == "date":
        value = coerce_scalar(field, field_mapping, raw)
        try:
            return encode_date(value)
        except MappingError as exc:
            raise InvalidQueryError(str(exc)) from exc
    raise UnsupportedFeatureError(
        f"range queries are not supported on field {field!r} (type {field_type!r})"
    )


def term_query(mapping: Mapping, analyzers: AnalyzerRegistry, field: str, raw: Any) -> Query:
    """Build the AST for an exact-match value against ``field``.

    A ``text`` field is analyzed: zero tokens yields :data:`NO_MATCH`, one
    token yields a single :class:`~scopiengine.query.ast.Term`, and more than
    one yields a :class:`~scopiengine.query.ast.Bool` OR of a
    :class:`~scopiengine.query.ast.Term` per token — the same "any analyzed
    token is enough" behaviour a Lucene-family ``match`` query gives a
    multi-token value. Every other indexable type is coerced and encoded via
    :mod:`scopiengine.mapping.fieldtypes`.

    An unmapped ``field`` still returns a well-formed
    :class:`~scopiengine.query.ast.Term`, exactly as if it *were* mapped — see
    the module docstring for why that is always safe.
    """
    field_mapping = mapping.fields.get(field)
    if field_mapping is None:
        return Term(field, raw if isinstance(raw, str) else str(raw))
    if field_mapping.type == "text":
        text = raw if isinstance(raw, str) else str(raw)
        tokens = analyzed_tokens(analyzers, field_mapping, text)
        if not tokens:
            return NO_MATCH
        if len(tokens) == 1:
            return Term(field, tokens[0])
        return Bool(should=tuple(Term(field, token) for token in tokens))

    value = coerce_scalar(field, field_mapping, raw)
    try:
        terms = terms_for_value(field_mapping.type, value, ignore_above=field_mapping.ignore_above)
    except MappingError as exc:
        raise InvalidQueryError(str(exc)) from exc
    if not terms:
        return NO_MATCH
    return Term(field, terms[0])


def phrase_query(mapping: Mapping, analyzers: AnalyzerRegistry, field: str, text: str) -> Query:
    """Build a :class:`~scopiengine.query.ast.Phrase` from analyzed ``text`` against ``field``.

    Raises:
        UnsupportedFeatureError: ``field`` is mapped but not as ``text``.
    """
    field_mapping = mapping.fields.get(field)
    if field_mapping is None:
        return Phrase(field, (text,))
    if field_mapping.type != "text":
        raise UnsupportedFeatureError(
            f"phrase queries require a text field; {field!r} is {field_mapping.type!r}"
        )
    tokens = analyzed_tokens(analyzers, field_mapping, text)
    if not tokens:
        return NO_MATCH
    return Phrase(field, tuple(tokens))


def prefix_query(mapping: Mapping, analyzers: AnalyzerRegistry, field: str, prefix: str) -> Query:
    """Build a :class:`~scopiengine.query.ast.Prefix` from ``prefix`` against ``field``.

    A ``text`` field is analyzed and only its *first* resulting token is used
    as the prefix — a multi-word prefix on a ``text`` field is out of scope
    for 1.0 (see ``docs/QUERY_LANGUAGE.md``). A ``keyword`` field's prefix is
    used verbatim, unanalyzed.

    Raises:
        UnsupportedFeatureError: ``field`` is mapped as neither ``text`` nor ``keyword``.
    """
    field_mapping = mapping.fields.get(field)
    if field_mapping is None:
        return Prefix(field, prefix)
    if field_mapping.type == "text":
        tokens = analyzed_tokens(analyzers, field_mapping, prefix)
        if not tokens:
            return NO_MATCH
        return Prefix(field, tokens[0])
    if field_mapping.type == "keyword":
        return Prefix(field, prefix)
    raise UnsupportedFeatureError(
        f"prefix queries are not supported on field {field!r} (type {field_mapping.type!r})"
    )


def range_query(
    mapping: Mapping,
    field: str,
    *,
    gte: Any = None,
    gt: Any = None,
    lte: Any = None,
    lt: Any = None,
) -> Query:
    """Build a :class:`~scopiengine.query.ast.Range` with bounds encoded for ``field``'s type."""
    field_mapping = mapping.fields.get(field)
    if field_mapping is None:
        return Range(field)
    return Range(
        field,
        gte=_encoded_bound(field, field_mapping, gte) if gte is not None else None,
        gt=_encoded_bound(field, field_mapping, gt) if gt is not None else None,
        lte=_encoded_bound(field, field_mapping, lte) if lte is not None else None,
        lt=_encoded_bound(field, field_mapping, lt) if lt is not None else None,
    )


def exists_query(field: str) -> Query:
    """Build an :class:`~scopiengine.query.ast.Exists` — safe for an unmapped field too."""
    return Exists(field)


def multi_field_term(mapping: Mapping, analyzers: AnalyzerRegistry, raw: str) -> Query:
    """A bare, unqualified term: matches if any ``text`` field matches it."""
    fields = text_fields(mapping)
    if not fields:
        return NO_MATCH
    return Bool(should=tuple(term_query(mapping, analyzers, name, raw) for name in fields))


def multi_field_phrase(mapping: Mapping, analyzers: AnalyzerRegistry, text: str) -> Query:
    """A bare, unqualified quoted phrase: matches if any ``text`` field matches it."""
    fields = text_fields(mapping)
    if not fields:
        return NO_MATCH
    return Bool(should=tuple(phrase_query(mapping, analyzers, name, text) for name in fields))
