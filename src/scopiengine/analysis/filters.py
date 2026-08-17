"""Token filters: the second stage of an :class:`~scopiengine.analysis.analyzer.Analyzer`.

A filter is a callable ``(tokens: Iterable[Token]) -> Iterator[Token]``. Filters
run in a chain, each consuming the previous one's output lazily. A filter may
rewrite a token's text, or drop it entirely by not yielding it — it must never
renumber the tokens it does yield, since :mod:`scopiengine.analysis.analyzer`
relies on the original tokenizer position surviving intact (see that module's
docstring for why removed tokens leave a positional gap rather than closing it).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable, Iterator

from scopiengine.analysis.stopwords import ENGLISH_STOPWORDS
from scopiengine.analysis.tokens import Token

__all__ = ["Filter", "asciifolding", "length", "lowercase", "stop", "truncate"]

#: A filter: a token stream in, a token stream out — text rewritten, or tokens dropped.
Filter = Callable[[Iterable[Token]], Iterator[Token]]


def lowercase(tokens: Iterable[Token]) -> Iterator[Token]:
    """Lowercase every token's text, Unicode-aware."""
    for token in tokens:
        yield Token(token.text.lower(), token.position)


def stop(words: Iterable[str] | None = None) -> Filter:
    """Build a filter that drops tokens matching a stop word list.

    Args:
        words: Stop words to drop, matched case-insensitively. Defaults to
            :data:`scopiengine.analysis.stopwords.ENGLISH_STOPWORDS`.

    Returns:
        A filter function.
    """
    blocked = ENGLISH_STOPWORDS if words is None else frozenset(w.lower() for w in words)

    def _filter(tokens: Iterable[Token]) -> Iterator[Token]:
        for token in tokens:
            if token.text.lower() not in blocked:
                yield token

    return _filter


def length(min_len: int = 0, max_len: int | None = None) -> Filter:
    """Build a filter that drops tokens outside ``[min_len, max_len]`` characters.

    Args:
        min_len: Shortest token length kept, inclusive.
        max_len: Longest token length kept, inclusive; ``None`` means unbounded.

    Returns:
        A filter function.
    """

    def _filter(tokens: Iterable[Token]) -> Iterator[Token]:
        for token in tokens:
            size = len(token.text)
            if size < min_len:
                continue
            if max_len is not None and size > max_len:
                continue
            yield token

    return _filter


def asciifolding(tokens: Iterable[Token]) -> Iterator[Token]:
    """Fold accented and other decomposable Unicode characters to plain ASCII.

    ``"café"`` becomes ``"cafe"``; characters with no ASCII decomposition (CJK
    ideographs, emoji, ...) pass through unchanged rather than being dropped.
    """
    for token in tokens:
        decomposed = unicodedata.normalize("NFKD", token.text)
        folded = decomposed.encode("ascii", "ignore").decode("ascii")
        yield Token(folded or token.text, token.position)


def truncate(n: int) -> Filter:
    """Build a filter that cuts every token down to its first ``n`` characters.

    Args:
        n: Maximum characters kept per token.

    Returns:
        A filter function.
    """
    if n < 1:
        raise ValueError(f"truncate length must be >= 1, got {n}")

    def _filter(tokens: Iterable[Token]) -> Iterator[Token]:
        for token in tokens:
            yield Token(token.text[:n], token.position)

    return _filter
