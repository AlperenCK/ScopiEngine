"""The token shape every tokenizer and filter in this package produces and consumes.

Keeping :class:`Token` in its own module (rather than in ``tokenizers.py`` or
``filters.py``) avoids a circular import between the two: both need the type,
neither owns it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Token"]


@dataclass(frozen=True, slots=True)
class Token:
    """One token on its way through an :class:`~scopiengine.analysis.analyzer.Analyzer`.

    Attributes:
        text: The token's current text — rewritten in place as filters run
            (lowercased, folded, truncated, ...).
        position: The token's ordinal position as the tokenizer emitted it, ``0``-based.
            Filters that drop tokens (:func:`~scopiengine.analysis.filters.stop`,
            :func:`~scopiengine.analysis.filters.length`) never renumber the
            survivors, so a removed token leaves a gap in the position sequence —
            see :mod:`scopiengine.analysis.analyzer` for why that gap matters.
    """

    text: str
    position: int
