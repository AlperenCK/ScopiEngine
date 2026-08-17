"""The built-in English stop word list used by the ``stop_en`` analyzer.

Kept in its own module so a caller can pass a different set to
:func:`scopiengine.analysis.filters.stop` without pulling in the rest of the
filters module, and so the list itself is easy to find and audit.
"""

from __future__ import annotations

__all__ = ["ENGLISH_STOPWORDS"]

#: A standard short English stop word list (the classic "Snowball"/Lucene set).
ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "if",
        "in",
        "into",
        "is",
        "it",
        "no",
        "not",
        "of",
        "on",
        "or",
        "such",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "will",
        "with",
    }
)
