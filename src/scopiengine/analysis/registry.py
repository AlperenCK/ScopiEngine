"""``AnalyzerRegistry``: named tokenizers, filters and analyzers, extensible by plugins.

The mapping layer resolves a field's ``analyzer``/``search_analyzer`` name through
one of these registries rather than importing :mod:`scopiengine.analysis.analyzer`
directly, so a plugin-registered analyzer (see
:mod:`scopiengine.plugins.hooks`, the ``analyzer`` hook) is indistinguishable from
a built-in one to the rest of the engine.
"""

from __future__ import annotations

from scopiengine.analysis import filters, tokenizers
from scopiengine.analysis.analyzer import Analyzer
from scopiengine.analysis.filters import Filter
from scopiengine.analysis.stopwords import ENGLISH_STOPWORDS
from scopiengine.analysis.tokenizers import Tokenizer
from scopiengine.errors import ConfigurationError

__all__ = ["AnalyzerRegistry"]


def _default_tokenizers() -> dict[str, Tokenizer]:
    #: Only whole-text tokenizers are named here; parametrised ones
    #: (:func:`~scopiengine.analysis.tokenizers.pattern`) are built per use.
    return {
        "standard": tokenizers.standard,
        "whitespace": tokenizers.whitespace,
        "keyword": tokenizers.keyword,
        "log": tokenizers.log,
    }


def _default_filters() -> dict[str, Filter]:
    return {
        "lowercase": filters.lowercase,
        "stop": filters.stop(),
        "asciifolding": filters.asciifolding,
    }


def _default_analyzers() -> dict[str, Analyzer]:
    return {
        "standard": Analyzer(tokenizers.standard, (filters.lowercase,)),
        "simple": Analyzer(tokenizers.pattern(r"[^A-Za-z]+"), (filters.lowercase,)),
        "whitespace": Analyzer(tokenizers.whitespace, ()),
        "keyword": Analyzer(tokenizers.keyword, ()),
        "stop_en": Analyzer(
            tokenizers.standard, (filters.lowercase, filters.stop(ENGLISH_STOPWORDS))
        ),
        "log": Analyzer(tokenizers.log, (filters.lowercase,)),
    }


class AnalyzerRegistry:
    """A mutable catalogue of named tokenizers, filters and analyzers.

    Seeded with the built-ins on construction. Plugins add to the same
    dictionaries a built-in would live in — there is no separate, privileged
    registration path for first-party analysis components.
    """

    def __init__(self) -> None:
        self._tokenizers: dict[str, Tokenizer] = _default_tokenizers()
        self._filters: dict[str, Filter] = _default_filters()
        self._analyzers: dict[str, Analyzer] = _default_analyzers()

    # -- registration ------------------------------------------------------

    def register_tokenizer(self, name: str, tokenizer: Tokenizer) -> None:
        """Register a tokenizer under ``name``, replacing any existing one."""
        self._tokenizers[name] = tokenizer

    def register_filter(self, name: str, filt: Filter) -> None:
        """Register a filter under ``name``, replacing any existing one."""
        self._filters[name] = filt

    def register_analyzer(self, name: str, analyzer: Analyzer) -> None:
        """Register a named analyzer, replacing any existing one."""
        self._analyzers[name] = analyzer

    # -- lookup --------------------------------------------------------------

    def tokenizer(self, name: str) -> Tokenizer:
        """Look up a tokenizer by name.

        Raises:
            ConfigurationError: No tokenizer is registered under ``name``.
        """
        try:
            return self._tokenizers[name]
        except KeyError:
            raise ConfigurationError(f"unknown tokenizer: {name!r}") from None

    def filter(self, name: str) -> Filter:
        """Look up a filter by name.

        Raises:
            ConfigurationError: No filter is registered under ``name``.
        """
        try:
            return self._filters[name]
        except KeyError:
            raise ConfigurationError(f"unknown filter: {name!r}") from None

    def analyzer(self, name: str) -> Analyzer:
        """Look up a named analyzer.

        Raises:
            ConfigurationError: No analyzer is registered under ``name``.
        """
        try:
            return self._analyzers[name]
        except KeyError:
            raise ConfigurationError(f"unknown analyzer: {name!r}") from None

    def analyzer_names(self) -> tuple[str, ...]:
        """List every registered analyzer name, sorted."""
        return tuple(sorted(self._analyzers))
