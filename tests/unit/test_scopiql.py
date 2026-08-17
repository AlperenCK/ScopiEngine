"""Every worked ScopiQL example parses to the expected AST; pipe stages parse to
the expected pipeline spec; and parse errors name the offending position and
what was expected there.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import InvalidQueryError
from scopiengine.mapping.encoding import encode_date, encode_long
from scopiengine.mapping.mapping import Mapping, parse_mapping
from scopiengine.query import scopiql
from scopiengine.query.ast import Bool, Exists, Phrase, Prefix, Range, Term

_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def mapping() -> Mapping:
    return parse_mapping(
        {
            "properties": {
                "level": {"type": "keyword"},
                "service": {"type": "keyword"},
                "status": {"type": "long"},
                "path": {"type": "keyword"},
                "message": {"type": "text"},
                "@timestamp": {"type": "date"},
                "trace_id": {"type": "keyword"},
            }
        }
    )


@pytest.fixture
def analyzers() -> AnalyzerRegistry:
    return AnalyzerRegistry()


def _parse(text: str, mapping: Mapping, analyzers: AnalyzerRegistry) -> scopiql.ParsedQuery:
    return scopiql.parse(text, mapping, analyzers, now=_NOW)


# -- the worked examples from the PR spec ------------------------------------


def test_implicit_and_between_two_field_terms(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    parsed = _parse("level:ERROR AND service:auth", mapping, analyzers)
    assert parsed.query == Bool(
        must=(Term("level", "ERROR"), Term("service", "auth")),
    )


def test_comparison_and_not(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("status:>=500 AND NOT path:/health", mapping, analyzers)
    # "AND NOT" flattens into one Bool's must/must_not (matching the equivalent
    # DSL bool query byte for byte) rather than nesting a lone must_not Bool
    # inside `must` — see `_combine_and`'s docstring for why that distinction
    # matters for scoring, not just for hits.
    assert parsed.query == Bool(
        must=(Range("status", gte=encode_long(500)),),
        must_not=(Term("path", "/health"),),
    )


def test_quoted_phrase(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse('message:"connection refused"', mapping, analyzers)
    assert parsed.query == Phrase("message", ("connection", "refused"))


def test_prefix_wildcard(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("message:conn*", mapping, analyzers)
    assert parsed.query == Prefix("message", "conn")


def test_field_value_group_is_or(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("level:(ERROR OR WARN OR FATAL)", mapping, analyzers)
    assert parsed.query == Bool(
        should=(Term("level", "ERROR"), Term("level", "WARN"), Term("level", "FATAL"))
    )


def test_inclusive_date_range(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("@timestamp:[2026-08-01 TO 2026-08-16]", mapping, analyzers)
    assert parsed.query == Range(
        "@timestamp",
        gte=encode_date("2026-08-01T00:00:00Z"),
        lte=encode_date("2026-08-16T00:00:00Z"),
    )


def test_relative_time(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("@timestamp:>now-1h", mapping, analyzers)
    from datetime import timedelta

    expected_gt = encode_date((_NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
    assert parsed.query == Range("@timestamp", gt=expected_gt)


def test_relative_time_plus(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    from datetime import timedelta

    parsed = _parse("@timestamp:<now+30m", mapping, analyzers)
    expected_lt = encode_date((_NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"))
    assert parsed.query == Range("@timestamp", lt=expected_lt)


def test_bare_now(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("@timestamp:>now", mapping, analyzers)
    expected_gt = encode_date(_NOW.isoformat().replace("+00:00", "Z"))
    assert parsed.query == Range("@timestamp", gt=expected_gt)


def test_exists(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("_exists_:trace_id", mapping, analyzers)
    assert parsed.query == Exists("trace_id")


def test_bare_phrase_matches_across_every_text_field(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    parsed = _parse('"payment failed"', mapping, analyzers)
    assert parsed.query == Bool(should=(Phrase("message", ("payment", "failed")),))


def test_bare_phrase_with_multiple_text_fields_ors_across_all_of_them(
    analyzers: AnalyzerRegistry,
) -> None:
    two_text_fields = parse_mapping(
        {"properties": {"title": {"type": "text"}, "body": {"type": "text"}}}
    )
    parsed = _parse('"payment failed"', two_text_fields, analyzers)
    assert parsed.query == Bool(
        should=(
            Phrase("body", ("payment", "failed")),
            Phrase("title", ("payment", "failed")),
        )
    )


def test_pipeline_sort_desc_and_limit(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse(
        "level:ERROR AND service:auth | sort -@timestamp | limit 20", mapping, analyzers
    )
    assert parsed.query == Bool(must=(Term("level", "ERROR"), Term("service", "auth")))
    assert parsed.sort == (scopiql.SortSpec(field="@timestamp", descending=True),)
    assert parsed.limit == 20


def test_pipeline_fields_and_limit(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("service:auth | fields service,status,message | limit 50", mapping, analyzers)
    assert parsed.query == Term("service", "auth")
    assert parsed.fields == ("service", "status", "message")
    assert parsed.limit == 50


def test_pipeline_stats_count_by(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("status:>=500 | stats count() by service", mapping, analyzers)
    assert parsed.query == Range("status", gte=encode_long(500))
    assert parsed.stats == scopiql.StatsSpec(by="service")


# -- additional grammar coverage ------------------------------------------


def test_empty_query_is_match_all(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    from scopiengine.query.ast import MatchAll

    assert _parse("", mapping, analyzers).query == MatchAll()
    assert _parse("   ", mapping, analyzers).query == MatchAll()


def test_ampersand_and_bang_shorthand(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("level:ERROR && !path:/health", mapping, analyzers)
    assert parsed.query == Bool(must=(Term("level", "ERROR"),), must_not=(Term("path", "/health"),))


def test_double_pipe_is_or(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("level:ERROR || level:WARN", mapping, analyzers)
    assert parsed.query == Bool(should=(Term("level", "ERROR"), Term("level", "WARN")))


def test_parentheses_group_boolean_expressions(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    parsed = _parse(
        "(level:ERROR OR level:FATAL) AND service:auth",
        mapping,
        analyzers,
    )
    assert parsed.query == Bool(
        must=(
            Bool(should=(Term("level", "ERROR"), Term("level", "FATAL"))),
            Term("service", "auth"),
        )
    )


def test_unmapped_field_still_parses_and_matches_nothing_at_search_time(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    parsed = _parse("no_such_field:whatever", mapping, analyzers)
    assert parsed.query == Term("no_such_field", "whatever")


def test_pipeline_multiple_sort_fields(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    parsed = _parse("level:ERROR | sort -status,service", mapping, analyzers)
    assert parsed.sort == (
        scopiql.SortSpec(field="status", descending=True),
        scopiql.SortSpec(field="service"),
    )


# -- parse errors name the position and what was expected --------------------


def test_error_after_dangling_colon_names_position_and_expectation(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse("level:", mapping, analyzers)
    err = exc_info.value
    assert err.detail["position"] == 6
    assert "value" in err.detail["expected"]
    assert err.detail["found"] is None
    assert "position 6" in err.reason


def test_error_after_dangling_and_names_end_of_input(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse("level:ERROR AND", mapping, analyzers)
    err = exc_info.value
    assert err.detail["position"] == 15
    assert err.detail["found"] is None


def test_error_missing_to_in_range_names_found_token(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse("@timestamp:[2026-08-01 2026-08-16]", mapping, analyzers)
    err = exc_info.value
    assert err.detail["expected"] == "'TO'"
    assert err.detail["found"] == "2026-08-16"
    assert err.detail["position"] == 23


def test_error_unbalanced_paren_names_position(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse("level:ERROR)", mapping, analyzers)
    err = exc_info.value
    assert err.detail["found"] == ")"
    assert err.detail["position"] == 11


def test_error_unknown_pipeline_stage_names_it(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse("level:ERROR | bogus", mapping, analyzers)
    err = exc_info.value
    assert err.detail["found"] == "bogus"


def test_error_stats_wrong_keyword_names_it(mapping: Mapping, analyzers: AnalyzerRegistry) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse("status:>=500 | stats count() over service", mapping, analyzers)
    err = exc_info.value
    assert err.detail["expected"] == "'by'"
    assert err.detail["found"] == "over"


def test_error_unterminated_string_names_the_position(
    mapping: Mapping, analyzers: AnalyzerRegistry
) -> None:
    with pytest.raises(InvalidQueryError) as exc_info:
        _parse('message:"unterminated', mapping, analyzers)
    err = exc_info.value
    assert err.detail["found"] == '"'
    assert err.detail["position"] == 8
    assert "unexpected character" in err.reason
