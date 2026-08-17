"""Tokenizers: the first stage of an :class:`~scopiengine.analysis.analyzer.Analyzer`.

A tokenizer is a callable ``(text: str) -> Iterator[Token]``. Every built-in here
assigns positions as a plain ``0``-based counter over the tokens it itself emits —
:mod:`scopiengine.analysis.analyzer` is the layer that later lets filters open gaps
in that sequence by dropping tokens.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

from scopiengine.analysis.tokens import Token

__all__ = ["Tokenizer", "keyword", "log", "pattern", "standard", "whitespace"]

#: A tokenizer: raw field text in, positioned tokens out.
Tokenizer = Callable[[str], Iterator[Token]]

#: Tokens longer than this are truncated (not dropped) — caps the cost of an
#: outlier like a base64 blob or a minified line without discarding the field.
_MAX_TOKEN_LENGTH = 255

#: Runs of Unicode letters/digits, excluding ``_`` — punctuation and whitespace
#: are the delimiters :func:`standard` splits on.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

#: Delimiters :func:`log` splits on, on top of whitespace: the punctuation that
#: commonly frames a field in a structured log line (``key=value``, ``[tag]``,
#: ``level: message``).
_LOG_DELIM_RE = re.compile(r"[\s=:\[\]]+", re.UNICODE)


def _clamp(text: str) -> str:
    """Truncate a token to :data:`_MAX_TOKEN_LENGTH` characters."""
    return text if len(text) <= _MAX_TOKEN_LENGTH else text[:_MAX_TOKEN_LENGTH]


def standard(text: str) -> Iterator[Token]:
    """Split on runs of Unicode word characters, dropping punctuation and whitespace.

    Each match is capped to :data:`_MAX_TOKEN_LENGTH` characters.
    """
    for position, match in enumerate(_WORD_RE.finditer(text)):
        yield Token(_clamp(match.group()), position)


def whitespace(text: str) -> Iterator[Token]:
    """Split on runs of whitespace; punctuation stays attached to its token."""
    for position, word in enumerate(text.split()):
        yield Token(_clamp(word), position)


def keyword(text: str) -> Iterator[Token]:
    """Emit the entire input as a single, unmodified token.

    Yields nothing for an empty string, so an absent field never produces a
    spurious empty term.
    """
    if text:
        yield Token(text, 0)


def pattern(regex: str, *, flags: int = 0) -> Tokenizer:
    """Build a tokenizer that splits text on a regular expression.

    Args:
        regex: Delimiter pattern. Matches are consumed as separators — the
            text between them becomes the tokens, mirroring how :func:`whitespace`
            treats runs of whitespace as separators.
        flags: Extra :mod:`re` flags, ORed with :data:`re.UNICODE`.

    Returns:
        A tokenizer function.
    """
    compiled = re.compile(regex, flags | re.UNICODE)

    def _tokenize(text: str) -> Iterator[Token]:
        position = 0
        for piece in compiled.split(text):
            if piece:
                yield Token(_clamp(piece), position)
                position += 1

    return _tokenize


def log(text: str) -> Iterator[Token]:
    """Split on whitespace plus ``=``, ``:``, ``[`` and ``]``.

    Turns a line like ``level=ERROR [auth] user:alice failed login`` into terms
    that line up with how log lines are typically grepped, rather than swallowing
    the key/value punctuation into the surrounding tokens.
    """
    position = 0
    for piece in _LOG_DELIM_RE.split(text):
        if piece:
            yield Token(_clamp(piece), position)
            position += 1
