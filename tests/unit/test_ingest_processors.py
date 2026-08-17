"""Built-in ingest processors: ``json_line``, ``regex_extract``, ``timestamp``."""

from __future__ import annotations

import json

from scopiengine.plugins.builtin.processors import (
    REGEX_PATTERN_CTX_KEY,
    json_line,
    regex_extract,
    register_ingest_processors,
    timestamp,
)


def test_registration_exposes_all_three_by_name() -> None:
    processors = register_ingest_processors()
    assert set(processors) == {"json_line", "regex_extract", "timestamp"}


# -- json_line -------------------------------------------------------------


def test_json_line_parses_a_json_object_into_fields() -> None:
    doc = {"message": json.dumps({"level": "ERROR", "service": "auth"})}
    result = json_line(doc, {})
    assert result == {"level": "ERROR", "service": "auth"}


def test_json_line_falls_back_to_raw_message_on_invalid_json() -> None:
    doc = {"message": "not json at all { }"}
    result = json_line(doc, {})
    assert result == doc


def test_json_line_falls_back_when_json_is_not_an_object() -> None:
    for literal in ("[1, 2, 3]", "42", '"just a string"', "true"):
        doc = {"message": literal}
        assert json_line(doc, {}) == doc


def test_json_line_leaves_a_missing_message_field_untouched() -> None:
    doc = {"other": "field"}
    assert json_line(doc, {}) == doc


# -- regex_extract -----------------------------------------------------------


def test_regex_extract_adds_named_groups_as_fields() -> None:
    pattern = r"^(?P<level>\w+): (?P<detail>.+)$"
    doc = {"message": "ERROR: connection refused"}
    result = regex_extract(doc, {REGEX_PATTERN_CTX_KEY: pattern})
    assert result["level"] == "ERROR"
    assert result["detail"] == "connection refused"
    assert result["message"] == doc["message"]  # original field preserved


def test_regex_extract_is_a_no_op_without_a_configured_pattern() -> None:
    doc = {"message": "ERROR: boom"}
    assert regex_extract(doc, {}) == doc


def test_regex_extract_is_a_no_op_when_the_pattern_does_not_match() -> None:
    pattern = r"^(?P<level>\w+): (?P<detail>.+)$"
    doc = {"message": "totally different shape"}
    assert regex_extract(doc, {REGEX_PATTERN_CTX_KEY: pattern}) == doc


def test_regex_extract_accepts_a_precompiled_pattern() -> None:
    import re

    compiled = re.compile(r"^(?P<code>\d+)")
    doc = {"message": "404 not found"}
    result = regex_extract(doc, {REGEX_PATTERN_CTX_KEY: compiled})
    assert result["code"] == "404"


def test_regex_extract_malformed_input_never_raises() -> None:
    pattern = r"^(?P<level>\w+):"
    for bad in ({"message": None}, {"message": 12345}, {}):
        assert regex_extract(dict(bad), {REGEX_PATTERN_CTX_KEY: pattern}) == bad


# -- timestamp ---------------------------------------------------------------


def test_timestamp_parses_iso8601_with_z_suffix() -> None:
    doc = {"timestamp": "2024-01-15T10:30:00Z"}
    result = timestamp(doc, {})
    assert result["@timestamp"] == "2024-01-15T10:30:00.000Z"


def test_timestamp_parses_iso8601_with_offset() -> None:
    doc = {"timestamp": "2024-01-15T10:30:00+02:00"}
    result = timestamp(doc, {})
    assert result["@timestamp"] == "2024-01-15T08:30:00.000Z"


def test_timestamp_parses_epoch_seconds() -> None:
    doc = {"ts": 1705315800}
    result = timestamp(doc, {})
    assert result["@timestamp"].startswith("2024-01-15T")


def test_timestamp_parses_epoch_milliseconds() -> None:
    doc = {"ts": 1705315800000}
    result = timestamp(doc, {})
    assert result["@timestamp"].startswith("2024-01-15T")


def test_timestamp_parses_apache_combined_log_format() -> None:
    doc = {"time": "15/Jan/2024:10:30:00 +0000"}
    result = timestamp(doc, {})
    assert result["@timestamp"] == "2024-01-15T10:30:00.000Z"


def test_timestamp_parses_classic_syslog_assuming_the_current_year() -> None:
    from datetime import UTC, datetime

    doc = {"time": "Jan 15 10:30:00"}
    result = timestamp(doc, {})
    assert result["@timestamp"].startswith(f"{datetime.now(UTC).year}-01-15T10:30:00")


def test_timestamp_field_priority_order_is_configurable() -> None:
    doc = {"timestamp": "2024-01-15T10:30:00Z", "custom_field": "2020-05-05T00:00:00Z"}
    result = timestamp(doc, {"timestamp_fields": ("custom_field", "timestamp")})
    assert result["@timestamp"].startswith("2020-05-05")


def test_timestamp_falls_back_to_a_leading_prefix_of_message() -> None:
    doc = {"message": "2024-01-15T10:30:00Z ERROR connection refused"}
    result = timestamp(doc, {})
    assert result["@timestamp"] == "2024-01-15T10:30:00.000Z"


def test_timestamp_malformed_input_leaves_the_document_unchanged() -> None:
    doc = {"timestamp": "definitely not a timestamp", "message": "also not one"}
    result = timestamp(doc, {})
    assert result == doc
    assert "@timestamp" not in result


def test_timestamp_ignores_a_boolean_masquerading_as_a_number() -> None:
    doc = {"timestamp": True}
    result = timestamp(doc, {})
    assert "@timestamp" not in result


def test_timestamp_skips_none_and_tries_the_next_field() -> None:
    doc = {"timestamp": None, "time": "2024-01-15T10:30:00Z"}
    result = timestamp(doc, {})
    assert result["@timestamp"] == "2024-01-15T10:30:00.000Z"
