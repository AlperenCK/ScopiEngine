"""Tests for tokenizers, filters and analyzer position handling."""

from __future__ import annotations

from scopiengine.analysis.analyzer import Analyzer
from scopiengine.analysis.filters import asciifolding, length, lowercase, stop, truncate
from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.analysis.stopwords import ENGLISH_STOPWORDS
from scopiengine.analysis.tokenizers import keyword, log, pattern, standard, whitespace
from scopiengine.analysis.tokens import Token

# Non-ASCII test fixtures, hoisted to module constants per the project's RUF001
# policy for ambiguous Unicode in string literals.
_CAFE = "café"
_NAIVE = "naïve"


# -- tokenizers ----------------------------------------------------------------


def test_standard_tokenizer_splits_on_punctuation_and_whitespace() -> None:
    tokens = list(standard("Hello, world! It's ScopiEngine-1.0."))
    assert [t.text for t in tokens] == ["Hello", "world", "It", "s", "ScopiEngine", "1", "0"]
    assert [t.position for t in tokens] == list(range(7))


def test_standard_tokenizer_caps_token_length() -> None:
    huge = "a" * 500
    (token,) = list(standard(huge))
    assert len(token.text) == 255


def test_standard_tokenizer_handles_unicode_letters() -> None:
    tokens = list(standard("münchen istanbul"))
    assert [t.text for t in tokens] == ["münchen", "istanbul"]


def test_whitespace_tokenizer_keeps_punctuation_attached() -> None:
    tokens = list(whitespace("error: connection refused!"))
    assert [t.text for t in tokens] == ["error:", "connection", "refused!"]
    assert [t.position for t in tokens] == [0, 1, 2]


def test_keyword_tokenizer_yields_whole_input_as_one_token() -> None:
    (token,) = list(keyword("  raw value, unsplit!  "))
    assert token.text == "  raw value, unsplit!  "
    assert token.position == 0


def test_keyword_tokenizer_yields_nothing_for_empty_string() -> None:
    assert list(keyword("")) == []


def test_pattern_tokenizer_splits_on_custom_delimiter() -> None:
    tokenizer = pattern(r"[,;]\s*")
    tokens = list(tokenizer("a, b; c,d"))
    assert [t.text for t in tokens] == ["a", "b", "c", "d"]
    assert [t.position for t in tokens] == [0, 1, 2, 3]


def test_log_tokenizer_splits_on_key_value_punctuation() -> None:
    tokens = list(log("level=ERROR [auth] user:alice failed login"))
    assert [t.text for t in tokens] == [
        "level",
        "ERROR",
        "auth",
        "user",
        "alice",
        "failed",
        "login",
    ]


# -- filters -------------------------------------------------------------------


def test_lowercase_filter() -> None:
    tokens = [Token("HELLO", 0), Token("World", 1)]
    assert [t.text for t in lowercase(tokens)] == ["hello", "world"]


def test_stop_filter_drops_matching_tokens_case_insensitively() -> None:
    tokens = [Token("The", 0), Token("Quick", 1), Token("THE", 2), Token("fox", 3)]
    filtered = list(stop(ENGLISH_STOPWORDS)(tokens))
    assert [t.text for t in filtered] == ["Quick", "fox"]


def test_length_filter_bounds() -> None:
    tokens = [Token("a", 0), Token("ab", 1), Token("abc", 2), Token("abcd", 3)]
    filtered = list(length(min_len=2, max_len=3)(tokens))
    assert [t.text for t in filtered] == ["ab", "abc"]


def test_asciifolding_strips_accents() -> None:
    tokens = [Token(_CAFE, 0), Token(_NAIVE, 1)]
    folded = list(asciifolding(tokens))
    assert [t.text for t in folded] == ["cafe", "naive"]


def test_truncate_filter_cuts_token_text() -> None:
    tokens = [Token("abcdef", 0)]
    (result,) = list(truncate(3)(tokens))
    assert result.text == "abc"
    assert result.position == 0


# -- analyzer: positional gaps ---------------------------------------------


def test_removed_stop_word_leaves_a_positional_gap() -> None:
    analyzer = Analyzer(standard, (lowercase, stop(ENGLISH_STOPWORDS)))
    result = list(analyzer.analyze("the quick brown fox"))
    # "the" (position 0) is removed; "quick" keeps its original position 1,
    # not renumbered down to 0 — see scopiengine.analysis.analyzer for why.
    assert result == [("quick", 1), ("brown", 2), ("fox", 3)]


def test_multiple_removed_tokens_each_leave_their_own_gap() -> None:
    analyzer = Analyzer(standard, (lowercase, stop(ENGLISH_STOPWORDS)))
    result = list(analyzer.analyze("a fox and a hound"))
    # "a"(0) "fox"(1) "and"(2) "a"(3) "hound"(4) -> "a"/"and"/"a" removed
    assert result == [("fox", 1), ("hound", 4)]


def test_analyzer_with_no_removals_has_contiguous_positions() -> None:
    analyzer = Analyzer(standard, (lowercase,))
    result = list(analyzer.analyze("quick brown fox"))
    assert result == [("quick", 0), ("brown", 1), ("fox", 2)]


def _blank_numbers(tokens: list[Token]) -> list[Token]:
    """A filter that empties out (rather than dropping) numeric tokens' text."""
    return [Token("" if token.text.isdigit() else token.text, token.position) for token in tokens]


def test_analyzer_drops_empty_terms_produced_by_filtering() -> None:
    analyzer = Analyzer(whitespace, (_blank_numbers,))
    result = list(analyzer.analyze("alpha 123 beta"))
    assert result == [("alpha", 0), ("beta", 2)]


# -- registry --------------------------------------------------------------


def test_registry_has_expected_builtin_analyzers() -> None:
    registry = AnalyzerRegistry()
    assert set(registry.analyzer_names()) == {
        "keyword",
        "log",
        "simple",
        "standard",
        "stop_en",
        "whitespace",
    }


def test_registry_custom_registration_is_used_by_lookup() -> None:
    registry = AnalyzerRegistry()
    custom = Analyzer(whitespace, ())
    registry.register_analyzer("mine", custom)
    assert registry.analyzer("mine") is custom


def test_stop_en_analyzer_end_to_end() -> None:
    registry = AnalyzerRegistry()
    result = list(registry.analyzer("stop_en").analyze("The Quick Fox"))
    assert result == [("quick", 1), ("fox", 2)]
