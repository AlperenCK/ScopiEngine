"""Every supported JSON DSL clause compiles to the expected AST; every
unsupported or misspelled clause/option raises :class:`UnsupportedFeatureError`
naming the exact key — never silently ignored.
"""

from __future__ import annotations

import pytest

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import InvalidQueryError, UnsupportedFeatureError
from scopiengine.mapping.encoding import encode_long
from scopiengine.mapping.mapping import Mapping, parse_mapping
from scopiengine.query import dsl
from scopiengine.query.ast import Bool, Exists, MatchAll, Phrase, Prefix, Range, Term


@pytest.fixture
def mapping() -> Mapping:
    return parse_mapping(
        {
            "properties": {
                "level": {"type": "keyword"},
                "service": {"type": "keyword"},
                "status": {"type": "long"},
                "message": {"type": "text"},
            }
        }
    )


@pytest.fixture
def analyzers() -> AnalyzerRegistry:
    return AnalyzerRegistry()


def _compile(body: dict, mapping: Mapping, analyzers: AnalyzerRegistry) -> dsl.DslQuery:
    return dsl.compile_dsl(mapping, analyzers, body)


# -- leaf clauses ----------------------------------------------------------


def test_match_all(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"match_all": {}}}, mapping, analyzers)
    assert parsed.query == MatchAll()


def test_missing_query_defaults_to_match_all(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({}, mapping, analyzers)
    assert parsed.query == MatchAll()


def test_match_shorthand(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"match": {"level": "ERROR"}}}, mapping, analyzers)
    assert parsed.query == Term("level", "ERROR")


def test_match_extended_form_with_boost(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile(
        {"query": {"match": {"level": {"query": "ERROR", "boost": 2.0}}}}, mapping, analyzers
    )
    assert parsed.query == Term("level", "ERROR", boost=2.0)


def test_match_phrase(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile(
        {"query": {"match_phrase": {"message": "connection refused"}}}, mapping, analyzers
    )
    assert parsed.query == Phrase("message", ("connection", "refused"))


def test_term(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"term": {"level": "ERROR"}}}, mapping, analyzers)
    assert parsed.query == Term("level", "ERROR")


def test_term_extended_form_uses_value_key(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"term": {"level": {"value": "ERROR"}}}}, mapping, analyzers)
    assert parsed.query == Term("level", "ERROR")


def test_terms(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"terms": {"level": ["ERROR", "WARN"]}}}, mapping, analyzers)
    assert parsed.query == Bool(should=(Term("level", "ERROR"), Term("level", "WARN")))


def test_terms_requires_non_empty_list(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    with pytest.raises(InvalidQueryError):
        _compile({"query": {"terms": {"level": []}}}, mapping, analyzers)


def test_prefix(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"prefix": {"message": "conn"}}}, mapping, analyzers)
    assert parsed.query == Prefix("message", "conn")


def test_range(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"range": {"status": {"gte": 500}}}}, mapping, analyzers)
    assert parsed.query == Range("status", gte=encode_long(500))


def test_range_with_all_four_bounds(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile(
        {"query": {"range": {"status": {"gte": 100, "lte": 599}}}}, mapping, analyzers
    )
    assert parsed.query == Range("status", gte=encode_long(100), lte=encode_long(599))


def test_exists(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"query": {"exists": {"field": "service"}}}, mapping, analyzers)
    assert parsed.query == Exists("service")


def test_bool_must_should_must_not_filter(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    body = {
        "query": {
            "bool": {
                "must": [{"term": {"level": "ERROR"}}],
                "filter": [{"range": {"status": {"gte": 500}}}],
                "must_not": [{"term": {"service": "healthcheck"}}],
                "should": [{"term": {"service": "auth"}}],
            }
        }
    }
    parsed = _compile(body, mapping, analyzers)
    assert parsed.query == Bool(
        must=(Term("level", "ERROR"),),
        should=(Term("service", "auth"),),
        must_not=(Term("service", "healthcheck"),),
        filter=(Range("status", gte=encode_long(500)),),
    )


def test_bool_accepts_a_single_clause_not_wrapped_in_a_list(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    parsed = _compile(
        {"query": {"bool": {"must": {"term": {"level": "ERROR"}}}}}, mapping, analyzers
    )
    assert parsed.query == Bool(must=(Term("level", "ERROR"),))


# -- top-level options -------------------------------------------------------


def test_from_and_size(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"from": 20, "size": 5}, mapping, analyzers)
    assert parsed.from_ == 20
    assert parsed.size == 5


def test_sort_string_shorthand(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"sort": ["-status", "service"]}, mapping, analyzers)
    assert parsed.sort[0].field == "status"
    assert parsed.sort[0].descending is True
    assert parsed.sort[1].field == "service"
    assert parsed.sort[1].descending is False


def test_sort_object_form(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"sort": [{"status": {"order": "desc"}}]}, mapping, analyzers)
    assert parsed.sort == (dsl.SortSpec(field="status", descending=True),)


def test_source_false_and_list(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    assert _compile({"_source": False}, mapping, analyzers).source is False
    assert _compile({"_source": ["level", "status"]}, mapping, analyzers).source == (
        "level",
        "status",
    )


def test_aggs_terms(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile({"aggs": {"by_service": {"terms": {"field": "service"}}}}, mapping, analyzers)
    assert parsed.aggs == {"by_service": dsl.TermsAgg(field="service")}


def test_aggs_terms_with_size(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile(
        {"aggs": {"by_service": {"terms": {"field": "service", "size": 3}}}}, mapping, analyzers
    )
    assert parsed.aggs == {"by_service": dsl.TermsAgg(field="service", size=3)}


def test_aggregations_alias_works_like_aggs(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _compile(
        {"aggregations": {"by_service": {"terms": {"field": "service"}}}}, mapping, analyzers
    )
    assert parsed.aggs == {"by_service": dsl.TermsAgg(field="service")}


# -- unsupported/misspelled keys raise, naming the exact key ------------------


def test_unsupported_top_level_option_names_it(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(UnsupportedFeatureError) as exc_info:
        _compile({"query": {"match_all": {}}, "minimum_should_match": 1}, mapping, analyzers)
    assert "minimum_should_match" in str(exc_info.value)


def test_unsupported_leaf_clause_names_it(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    with pytest.raises(UnsupportedFeatureError) as exc_info:
        _compile({"query": {"fuzzy": {"message": "conn"}}}, mapping, analyzers)
    assert "fuzzy" in str(exc_info.value)


def test_unsupported_bool_option_names_it(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    with pytest.raises(UnsupportedFeatureError) as exc_info:
        _compile({"query": {"bool": {"must": [], "minimum_should_match": 1}}}, mapping, analyzers)
    assert "minimum_should_match" in str(exc_info.value)


def test_unsupported_range_option_names_it(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    with pytest.raises(UnsupportedFeatureError) as exc_info:
        _compile(
            {"query": {"range": {"status": {"gte": 1, "format": "epoch_millis"}}}},
            mapping,
            analyzers,
        )
    assert "format" in str(exc_info.value)


def test_unsupported_aggregation_type_names_the_agg(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(UnsupportedFeatureError) as exc_info:
        _compile({"aggs": {"avg_status": {"avg": {"field": "status"}}}}, mapping, analyzers)
    assert "avg_status" in str(exc_info.value)


def test_unsupported_terms_agg_option_names_it(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(UnsupportedFeatureError) as exc_info:
        _compile(
            {"aggs": {"by_service": {"terms": {"field": "service", "order": {"_count": "asc"}}}}},
            mapping,
            analyzers,
        )
    assert "order" in str(exc_info.value)


def test_both_aggs_and_aggregations_is_rejected(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError):
        _compile(
            {"aggs": {"a": {"terms": {"field": "level"}}}, "aggregations": {}}, mapping, analyzers
        )
