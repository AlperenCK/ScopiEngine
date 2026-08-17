"""The query AST executed by :mod:`scopiengine.index.searcher`.

This is deliberately the whole of the query layer for now: PR 4 adds the ScopiQL
and JSON-DSL parsers that build these nodes from user input. Every node is a
frozen dataclass — an AST is a value, never mutated once built — and terms are
expected to already be in their final, matchable form (an analyzed token for a
``text`` field, or the order-preserving encoded term from
:mod:`scopiengine.mapping.encoding` for ``long``/``double``/``date``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Bool", "Exists", "MatchAll", "Phrase", "Prefix", "Query", "Range", "Term"]


@dataclass(frozen=True, slots=True)
class MatchAll:
    """Matches every live document, each with a constant score of ``1.0``."""


@dataclass(frozen=True, slots=True)
class Term:
    """Matches documents where ``field`` has an exact-match occurrence of ``value``.

    Attributes:
        field: Dotted field path.
        value: The exact term to match — already analyzed/encoded by the caller.
        boost: Multiplier applied to this clause's BM25 score.
    """

    field: str
    value: str
    boost: float = 1.0


@dataclass(frozen=True, slots=True)
class Phrase:
    """Matches documents where ``terms`` occur consecutively (subject to ``slop``).

    Requires the field to be mapped with ``index_options="positions"``.

    Attributes:
        field: Dotted field path.
        terms: The phrase's terms, in query order, already analyzed.
        slop: Maximum total position distance allowed between the terms; ``0``
            requires them to appear exactly consecutively, in order.
        boost: Multiplier applied to this clause's BM25 score.
    """

    field: str
    terms: tuple[str, ...]
    slop: int = 0
    boost: float = 1.0


@dataclass(frozen=True, slots=True)
class Prefix:
    """Matches documents where ``field`` has any term starting with ``value``.

    Attributes:
        field: Dotted field path.
        value: The prefix to match, already analyzed/encoded.
        boost: Multiplier applied to this clause's BM25 score.
    """

    field: str
    value: str
    boost: float = 1.0


@dataclass(frozen=True, slots=True)
class Range:
    """Matches documents where ``field``'s term falls within the given bounds.

    Bounds are compared as encoded term strings — for ``long``/``double``/``date``
    fields the caller must encode them with :mod:`scopiengine.mapping.encoding`
    first, so the order-preserving invariant carries through to the query side.
    At least one bound should be set; all unset means every term matches.

    Attributes:
        field: Dotted field path.
        gte: Inclusive lower bound.
        gt: Exclusive lower bound (ignored if ``gte`` is also set).
        lte: Inclusive upper bound.
        lt: Exclusive upper bound (ignored if ``lte`` is also set).
        boost: Multiplier applied to this clause's BM25 score.
    """

    field: str
    gte: str | None = None
    gt: str | None = None
    lte: str | None = None
    lt: str | None = None
    boost: float = 1.0


@dataclass(frozen=True, slots=True)
class Exists:
    """Matches documents that have any indexed value at all for ``field``."""

    field: str


@dataclass(frozen=True, slots=True)
class Bool:
    """Combines other query nodes with boolean logic.

    Attributes:
        must: Every clause must match; each contributes to the score.
        should: Clauses that boost the score when they match. When ``must`` and
            ``filter`` are both empty, at least one ``should`` clause must match
            (this is the whole query in that case).
        must_not: No clause may match; contributes nothing to the score.
        filter: Every clause must match, like ``must``, but contributes no score.
    """

    must: tuple[Query, ...] = field(default_factory=tuple)
    should: tuple[Query, ...] = field(default_factory=tuple)
    must_not: tuple[Query, ...] = field(default_factory=tuple)
    filter: tuple[Query, ...] = field(default_factory=tuple)


#: Any node in the query AST.
Query = MatchAll | Term | Phrase | Prefix | Range | Exists | Bool
