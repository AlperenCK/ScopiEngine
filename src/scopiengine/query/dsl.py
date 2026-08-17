"""The Elasticsearch-shaped JSON query DSL — a deliberate subset, compiled to
the exact same :mod:`scopiengine.query.ast` that :mod:`scopiengine.query.scopiql`
builds, through the identical field-resolution helpers in
:mod:`scopiengine.query.compiler`.

Every clause and every top-level search option this module does not recognise
raises :class:`~scopiengine.errors.UnsupportedFeatureError` naming the exact
key — never silently ignored, so a request can never quietly return the wrong
results because a key was misspelled or simply not implemented yet. See
``docs/QUERY_LANGUAGE.md`` for the full supported/unsupported compatibility
matrix.

Supported
---------
Leaf queries: ``match_all``, ``match``, ``match_phrase``, ``term``, ``terms``,
``prefix``, ``range``, ``exists``, ``bool`` (``must``/``should``/``must_not``/
``filter``, plus ``boost``). Top level: ``query``, ``from``, ``size``,
``sort``, ``_source``, ``aggs``/``aggregations`` (``terms`` only).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import InvalidQueryError, UnsupportedFeatureError
from scopiengine.mapping.mapping import Mapping
from scopiengine.query import compiler
from scopiengine.query.ast import Bool, MatchAll, Phrase, Prefix, Query, Range, Term
from scopiengine.query.scopiql import SortSpec

__all__ = ["DslQuery", "TermsAgg", "compile_dsl"]


@dataclass(frozen=True, slots=True)
class TermsAgg:
    """A ``terms`` aggregation: bucket by a field's distinct values, counted.

    Attributes:
        field: Field path to bucket by.
        size: Maximum number of buckets returned, ranked by count descending.
    """

    field: str
    size: int = 10


@dataclass(frozen=True, slots=True)
class DslQuery:
    """Everything one JSON DSL request body compiles to.

    Attributes:
        query: The compiled :mod:`scopiengine.query.ast` query.
        from_: Number of top-ranked hits to skip.
        size: Maximum number of hits to return.
        sort: Requested sort order; empty means "relevance" (BM25 score).
        source: ``True`` for the whole ``_source``, ``False`` to omit it
            entirely, or a tuple of field paths to project.
        aggs: Named aggregation specs, by aggregation name.
    """

    query: Query
    from_: int = 0
    size: int = 10
    sort: tuple[SortSpec, ...] = ()
    source: bool | tuple[str, ...] = True
    aggs: dict[str, TermsAgg] = field(default_factory=dict)


# -- boost propagation ---------------------------------------------------------


def _with_boost(query: Query, boost: float) -> Query:
    """Multiply ``boost`` into every leaf of ``query`` that carries a ``boost`` field."""
    if boost == 1.0:
        return query
    if isinstance(query, Term | Phrase | Prefix | Range):
        return dataclasses.replace(query, boost=query.boost * boost)
    if isinstance(query, Bool):
        return dataclasses.replace(
            query,
            must=tuple(_with_boost(q, boost) for q in query.must),
            should=tuple(_with_boost(q, boost) for q in query.should),
            filter=tuple(_with_boost(q, boost) for q in query.filter),
        )
    return query


# -- field/value extraction ------------------------------------------------


def _single_field(value: Any, clause: str) -> tuple[str, Any]:
    """Unwrap a ``{"field": ...}`` single-key object, as most leaf clauses take."""
    if not isinstance(value, dict) or len(value) != 1:
        raise InvalidQueryError(f"{clause!r} query must map exactly one field to a value")
    ((field_name, raw),) = value.items()
    return field_name, raw


def _field_query_and_boost(value: Any, clause: str, value_key: str) -> tuple[str, Any, float]:
    """Unwrap ``{"field": v}`` or the extended ``{"field": {value_key: v, "boost": b}}`` form."""
    field_name, raw = _single_field(value, clause)
    if isinstance(raw, dict):
        if value_key not in raw and "value" not in raw:
            raise InvalidQueryError(
                f"{clause!r} query for field {field_name!r} is missing {value_key!r}"
            )
        actual = raw[value_key] if value_key in raw else raw["value"]
        boost = raw.get("boost", 1.0)
        if isinstance(boost, bool) or not isinstance(boost, int | float):
            raise InvalidQueryError(f"{clause!r} query 'boost' must be a number")
        return field_name, actual, float(boost)
    return field_name, raw, 1.0


# -- leaf query clauses ----------------------------------------------------


def _compile_match(mapping: Mapping, analyzers: AnalyzerRegistry, value: Any) -> Query:
    field_name, raw, boost = _field_query_and_boost(value, "match", "query")
    return _with_boost(compiler.term_query(mapping, analyzers, field_name, raw), boost)


def _compile_match_phrase(mapping: Mapping, analyzers: AnalyzerRegistry, value: Any) -> Query:
    field_name, raw, boost = _field_query_and_boost(value, "match_phrase", "query")
    if not isinstance(raw, str):
        raise InvalidQueryError(f"match_phrase query for field {field_name!r} must be a string")
    return _with_boost(compiler.phrase_query(mapping, analyzers, field_name, raw), boost)


def _compile_term(mapping: Mapping, analyzers: AnalyzerRegistry, value: Any) -> Query:
    field_name, raw, boost = _field_query_and_boost(value, "term", "value")
    return _with_boost(compiler.term_query(mapping, analyzers, field_name, raw), boost)


def _compile_terms(mapping: Mapping, analyzers: AnalyzerRegistry, value: Any) -> Query:
    if not isinstance(value, dict):
        raise InvalidQueryError("'terms' query must be a JSON object")
    boost = value.get("boost", 1.0)
    if isinstance(boost, bool) or not isinstance(boost, int | float):
        raise InvalidQueryError("'terms' query 'boost' must be a number")
    fields = {key: v for key, v in value.items() if key != "boost"}
    field_name, raw = _single_field(fields, "terms")
    if not isinstance(raw, list) or not raw:
        raise InvalidQueryError(f"'terms' query for field {field_name!r} must be a non-empty list")
    clauses = tuple(compiler.term_query(mapping, analyzers, field_name, v) for v in raw)
    query: Query = clauses[0] if len(clauses) == 1 else Bool(should=clauses)
    return _with_boost(query, float(boost))


def _compile_prefix(mapping: Mapping, analyzers: AnalyzerRegistry, value: Any) -> Query:
    field_name, raw, boost = _field_query_and_boost(value, "prefix", "value")
    if not isinstance(raw, str):
        raise InvalidQueryError(f"'prefix' query for field {field_name!r} must be a string")
    return _with_boost(compiler.prefix_query(mapping, analyzers, field_name, raw), boost)


_RANGE_OPTIONS = frozenset({"gte", "gt", "lte", "lt", "boost"})


def _compile_range(mapping: Mapping, value: Any) -> Query:
    field_name, bounds = _single_field(value, "range")
    if not isinstance(bounds, dict):
        raise InvalidQueryError(f"'range' query for field {field_name!r} must be a JSON object")
    unknown = set(bounds) - _RANGE_OPTIONS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported range option(s) for field {field_name!r}: {sorted(unknown)}"
        )
    boost = bounds.get("boost", 1.0)
    if isinstance(boost, bool) or not isinstance(boost, int | float):
        raise InvalidQueryError("'range' query 'boost' must be a number")
    query = compiler.range_query(
        mapping,
        field_name,
        gte=bounds.get("gte"),
        gt=bounds.get("gt"),
        lte=bounds.get("lte"),
        lt=bounds.get("lt"),
    )
    return _with_boost(query, float(boost))


def _compile_exists(value: Any) -> Query:
    if not isinstance(value, dict) or "field" not in value:
        raise InvalidQueryError("'exists' query requires a 'field' key")
    field_name = value["field"]
    if not isinstance(field_name, str):
        raise InvalidQueryError("'exists' query 'field' must be a string")
    return compiler.exists_query(field_name)


_BOOL_OPTIONS = frozenset({"must", "should", "must_not", "filter", "boost"})


def _compile_bool(mapping: Mapping, analyzers: AnalyzerRegistry, value: Any) -> Query:
    if not isinstance(value, dict):
        raise InvalidQueryError("'bool' query must be a JSON object")
    unknown = set(value) - _BOOL_OPTIONS
    if unknown:
        raise UnsupportedFeatureError(f"unsupported 'bool' option(s): {sorted(unknown)}")

    def clause_list(key: str) -> tuple[Query, ...]:
        raw = value.get(key, [])
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise InvalidQueryError(f"'bool.{key}' must be a query clause or a list of clauses")
        return tuple(_compile_clause(mapping, analyzers, clause) for clause in raw)

    boost = value.get("boost", 1.0)
    if isinstance(boost, bool) or not isinstance(boost, int | float):
        raise InvalidQueryError("'bool' query 'boost' must be a number")
    query = Bool(
        must=clause_list("must"),
        should=clause_list("should"),
        must_not=clause_list("must_not"),
        filter=clause_list("filter"),
    )
    return _with_boost(query, float(boost))


def _compile_clause(mapping: Mapping, analyzers: AnalyzerRegistry, clause: Any) -> Query:
    if not isinstance(clause, dict) or len(clause) != 1:
        raise InvalidQueryError("a query clause must be a JSON object with exactly one key")
    ((key, value),) = clause.items()
    if key == "match_all":
        return MatchAll()
    if key == "match":
        return _compile_match(mapping, analyzers, value)
    if key == "match_phrase":
        return _compile_match_phrase(mapping, analyzers, value)
    if key == "term":
        return _compile_term(mapping, analyzers, value)
    if key == "terms":
        return _compile_terms(mapping, analyzers, value)
    if key == "prefix":
        return _compile_prefix(mapping, analyzers, value)
    if key == "range":
        return _compile_range(mapping, value)
    if key == "exists":
        return _compile_exists(value)
    if key == "bool":
        return _compile_bool(mapping, analyzers, value)
    raise UnsupportedFeatureError(f"unsupported query clause: {key!r}")


# -- top-level search options -----------------------------------------------


def _int_option(body: dict[str, Any], key: str, *, default: int) -> int:
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidQueryError(f"{key!r} must be an integer")
    return value


def _compile_sort(raw: Any) -> tuple[SortSpec, ...]:
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    specs: list[SortSpec] = []
    for item in items:
        if isinstance(item, str):
            if item.startswith("-"):
                specs.append(SortSpec(field=item[1:], descending=True))
            else:
                specs.append(SortSpec(field=item))
            continue
        if isinstance(item, dict) and len(item) == 1:
            ((field_name, order),) = item.items()
            if isinstance(order, str) and order.lower() in ("asc", "desc"):
                specs.append(SortSpec(field=field_name, descending=order.lower() == "desc"))
                continue
            if isinstance(order, dict) and isinstance(order.get("order"), str):
                specs.append(
                    SortSpec(field=field_name, descending=order["order"].lower() == "desc")
                )
                continue
        raise InvalidQueryError(f"unsupported 'sort' entry: {item!r}")
    return tuple(specs)


def _compile_source(raw: Any) -> bool | tuple[str, ...]:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        if not all(isinstance(item, str) for item in raw):
            raise InvalidQueryError("'_source' list must contain only field names")
        return tuple(raw)
    raise InvalidQueryError("'_source' must be a boolean, a field name, or a list of field names")


_TERMS_AGG_OPTIONS = frozenset({"field", "size"})


def _compile_aggs(raw: Any) -> dict[str, TermsAgg]:
    if not isinstance(raw, dict):
        raise InvalidQueryError("'aggs' must be a JSON object")
    result: dict[str, TermsAgg] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or set(spec) != {"terms"}:
            raise UnsupportedFeatureError(
                f"unsupported aggregation type for {name!r}: only 'terms' is supported"
            )
        terms_spec = spec["terms"]
        if not isinstance(terms_spec, dict) or "field" not in terms_spec:
            raise InvalidQueryError(f"aggregation {name!r}.terms requires a 'field'")
        unknown = set(terms_spec) - _TERMS_AGG_OPTIONS
        if unknown:
            raise UnsupportedFeatureError(
                f"unsupported terms aggregation option(s) for {name!r}: {sorted(unknown)}"
            )
        field_name = terms_spec["field"]
        if not isinstance(field_name, str):
            raise InvalidQueryError(f"aggregation {name!r}.terms.field must be a string")
        size = terms_spec.get("size", 10)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise InvalidQueryError(f"aggregation {name!r}.terms.size must be a positive integer")
        result[name] = TermsAgg(field=field_name, size=size)
    return result


_TOP_LEVEL_OPTIONS = frozenset({"query", "from", "size", "sort", "_source", "aggs", "aggregations"})


def compile_dsl(mapping: Mapping, analyzers: AnalyzerRegistry, body: dict[str, Any]) -> DslQuery:
    """Compile one JSON DSL request body to a :class:`DslQuery`.

    Raises:
        InvalidQueryError: ``body`` (or a clause within it) is malformed.
        UnsupportedFeatureError: ``body`` names a clause or option this
            module does not implement — the error names the exact key.
    """
    if not isinstance(body, dict):
        raise InvalidQueryError("request body must be a JSON object")
    unknown = set(body) - _TOP_LEVEL_OPTIONS
    if unknown:
        raise UnsupportedFeatureError(f"unsupported search option(s): {sorted(unknown)}")
    if "aggs" in body and "aggregations" in body:
        raise InvalidQueryError("specify only one of 'aggs' or 'aggregations'")

    query_clause = body.get("query")
    query: Query = (
        _compile_clause(mapping, analyzers, query_clause)
        if query_clause is not None
        else MatchAll()
    )

    from_ = _int_option(body, "from", default=0)
    size = _int_option(body, "size", default=10)
    if from_ < 0:
        raise InvalidQueryError("'from' must be >= 0")
    if size < 0:
        raise InvalidQueryError("'size' must be >= 0")

    sort = _compile_sort(body.get("sort"))
    source = _compile_source(body.get("_source", True))
    aggs = _compile_aggs(body.get("aggs", body.get("aggregations", {})))

    return DslQuery(query=query, from_=from_, size=size, sort=sort, source=source, aggs=aggs)
