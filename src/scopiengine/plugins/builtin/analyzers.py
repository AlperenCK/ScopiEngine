"""Registers the built-in tokenizers, filters and analyzers.

:class:`~scopiengine.analysis.registry.AnalyzerRegistry` already seeds these same
names on construction (so a bare ``AnalyzerRegistry()`` is useful without going
through plugin discovery at all — useful for tests and for embedding the
analysis layer standalone). This module re-registers them through the ``analyzer``
hook so that path is genuinely exercised by :func:`scopiengine.plugins.registry.discover_plugins`,
and so this file is the reference a plugin author copies to add their own
tokenizer, filter or analyzer — reference-implementation quality, not a
special case.
"""

from __future__ import annotations

from scopiengine.analysis import filters, tokenizers
from scopiengine.analysis.analyzer import Analyzer
from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.analysis.stopwords import ENGLISH_STOPWORDS

__all__ = ["register_analyzers"]


def register_analyzers(registry: AnalyzerRegistry) -> None:
    """Register the standard/simple/whitespace/keyword/stop_en/log analyzers."""
    registry.register_tokenizer("standard", tokenizers.standard)
    registry.register_tokenizer("whitespace", tokenizers.whitespace)
    registry.register_tokenizer("keyword", tokenizers.keyword)
    registry.register_tokenizer("log", tokenizers.log)

    registry.register_filter("lowercase", filters.lowercase)
    registry.register_filter("stop", filters.stop())
    registry.register_filter("asciifolding", filters.asciifolding)

    registry.register_analyzer("standard", Analyzer(tokenizers.standard, (filters.lowercase,)))
    registry.register_analyzer(
        "simple", Analyzer(tokenizers.pattern(r"[^A-Za-z]+"), (filters.lowercase,))
    )
    registry.register_analyzer("whitespace", Analyzer(tokenizers.whitespace, ()))
    registry.register_analyzer("keyword", Analyzer(tokenizers.keyword, ()))
    registry.register_analyzer(
        "stop_en",
        Analyzer(tokenizers.standard, (filters.lowercase, filters.stop(ENGLISH_STOPWORDS))),
    )
    registry.register_analyzer("log", Analyzer(tokenizers.log, (filters.lowercase,)))
