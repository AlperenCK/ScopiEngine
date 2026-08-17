"""``Analyzer``: a tokenizer plus an ordered filter chain, producing ``(term, position)`` pairs.

Position policy
----------------
The tokenizer assigns each raw token a ``0``-based position as it emits it. Every
filter after that either passes a token through (unchanged position) or drops it —
none of them renumber what survives. So when :func:`~scopiengine.analysis.filters.stop`
removes ``"the"`` from ``"the quick fox"``, ``"quick"`` keeps position ``1`` and
``"fox"`` keeps position ``2``; the pair emitted is ``[("quick", 1), ("fox", 2)]``,
not ``[("quick", 0), ("fox", 1)]``.

That gap is deliberate. A phrase query for ``"quick fox"`` asks for two terms at
adjacent positions (a distance of ``1``) — and after removing a stop word that
distance really is ``1`` less than the words' distance in the original text, but a
literal count of *surviving* tokens would collapse it to looking adjacent when it
is not. Closing the gap would make ``"the quick"`` phrase-match text that only
became adjacent because a word was silently discarded — exactly the kind of
quietly-wrong result this project's error hierarchy exists to avoid. Preserving
the original position keeps phrase distance meaning what it says.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from scopiengine.analysis.filters import Filter
from scopiengine.analysis.tokenizers import Tokenizer
from scopiengine.analysis.tokens import Token

__all__ = ["Analyzer"]


@dataclass(frozen=True, slots=True)
class Analyzer:
    """A tokenizer followed by zero or more filters, run in order.

    Attributes:
        tokenizer: Splits raw text into positioned tokens.
        filters: Applied left to right; each consumes the previous stage's output.
    """

    tokenizer: Tokenizer
    filters: tuple[Filter, ...] = field(default_factory=tuple)

    def analyze(self, text: str) -> Iterator[tuple[str, int]]:
        """Tokenize and filter ``text``, yielding ``(term, position)`` pairs.

        Empty terms (text that filtered down to ``""``) are dropped — an empty
        string is never a meaningful term to index or search on.
        """
        stream: Iterator[Token] = self.tokenizer(text)
        for filt in self.filters:
            stream = filt(stream)
        for token in stream:
            if token.text:
                yield token.text, token.position
