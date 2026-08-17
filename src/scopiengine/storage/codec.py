"""Backend-agnostic byte encoding for postings lists and norms.

Every storage backend stores these bytes verbatim in a ``BLOB``/``VARBINARY`` column;
none of them know the layout. Keeping the codec here — rather than duplicated per
backend — is what makes a term's postings byte-identical regardless of where they
are stored.

Postings blob layout::

    [magic u8][flags u8][doc_count varint]
    per document:
        docid_gap varint
        tf varint
        if positions flag set: tf position-gap varints

Both the document ordinal gaps and the position gaps are unsigned LEB128 varints,
delta-encoded against the previous value (starting from zero), which is what keeps
a real-world postings list small: gaps between nearby ordinals are the common case.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from scopiengine.errors import StorageError
from scopiengine.storage.models import Posting

__all__ = ["decode_norms", "decode_postings", "encode_norms", "encode_postings"]

#: Identifies this module's postings format, so a misrouted blob fails fast.
_MAGIC = 0x53
#: Set in the flags byte when each posting carries token positions.
_FLAG_POSITIONS = 0x01
#: Norms are clamped to a single byte, matching Lucene-style length encoding.
_NORM_MAX = 255


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as an unsigned LEB128 varint."""
    if value < 0:
        raise StorageError(f"cannot encode a negative varint: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(blob: bytes, pos: int) -> tuple[int, int]:
    """Decode one unsigned LEB128 varint from ``blob`` starting at ``pos``.

    Returns:
        The decoded value and the position just past it.

    Raises:
        StorageError: The blob ends before a complete varint is read.
    """
    result = 0
    shift = 0
    while True:
        if pos >= len(blob):
            raise StorageError("truncated postings blob: varint runs past end of buffer")
        byte = blob[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def encode_postings(
    postings: Iterable[tuple[int, int, tuple[int, ...]]],
    *,
    with_positions: bool,
) -> bytes:
    """Encode a term's postings list as a self-describing byte blob.

    Args:
        postings: ``(doc_ord, tf, positions)`` triples in strictly ascending
            ``doc_ord`` order. ``positions`` is ignored when ``with_positions`` is
            ``False``, and must itself be ascending when it is used.
        with_positions: Whether to encode per-occurrence token positions.

    Returns:
        The encoded blob.

    Raises:
        StorageError: ``doc_ord`` (or, when positions are encoded, ``positions``)
            is not strictly ascending, or a posting's position count disagrees
            with its term frequency.
    """
    body = bytearray()
    doc_count = 0
    prev_ord = -1
    for doc_ord, tf, positions in postings:
        if doc_ord <= prev_ord:
            raise StorageError(
                f"postings must be in strictly ascending doc_ord order: "
                f"{doc_ord} follows {prev_ord}"
            )
        body += _encode_varint(doc_ord - prev_ord)
        body += _encode_varint(tf)
        if with_positions:
            # The decoder reads exactly tf position gaps, so a mismatch here would
            # silently desynchronise every posting after this one.
            if len(positions) != tf:
                raise StorageError(
                    f"doc_ord {doc_ord} declares tf={tf} but carries {len(positions)} positions"
                )
            prev_pos = -1
            for position in positions:
                if position <= prev_pos:
                    raise StorageError(
                        f"positions must be strictly ascending: {position} follows {prev_pos}"
                    )
                body += _encode_varint(position - prev_pos)
                prev_pos = position
        prev_ord = doc_ord
        doc_count += 1

    header = bytes([_MAGIC, _FLAG_POSITIONS if with_positions else 0]) + _encode_varint(doc_count)
    return header + bytes(body)


def decode_postings(blob: bytes) -> Iterator[Posting]:
    """Decode a postings blob produced by :func:`encode_postings`.

    The header is validated eagerly, so a misrouted or empty blob is reported at
    the call site rather than on first iteration. The body is then decoded lazily:
    no byte past the header is touched until the caller asks for the next
    :class:`~scopiengine.storage.models.Posting`, so a term matching millions of
    documents never materialises a list.

    Args:
        blob: A blob produced by :func:`encode_postings`.

    Returns:
        An iterator yielding one :class:`~scopiengine.storage.models.Posting` per
        encoded document, with ``doc_ord`` reconstructed to the absolute ordinal.

    Raises:
        StorageError: The blob is empty or carries an unknown magic byte. A blob
            truncated partway through a record raises during iteration instead.
    """
    if len(blob) < 2:
        raise StorageError("truncated postings blob: missing header")
    magic, flags = blob[0], blob[1]
    if magic != _MAGIC:
        raise StorageError(f"unrecognised postings blob: bad magic byte {magic:#x}")
    with_positions = bool(flags & _FLAG_POSITIONS)
    doc_count, body_start = _decode_varint(blob, 2)
    return _iter_postings(blob, body_start, doc_count, with_positions)


def _iter_postings(
    blob: bytes, pos: int, doc_count: int, with_positions: bool
) -> Iterator[Posting]:
    """Yield postings from an already-validated blob body."""
    doc_ord = -1
    for _ in range(doc_count):
        gap, pos = _decode_varint(blob, pos)
        doc_ord += gap
        tf, pos = _decode_varint(blob, pos)
        positions: tuple[int, ...] = ()
        if with_positions:
            collected = []
            position = -1
            for _p in range(tf):
                pgap, pos = _decode_varint(blob, pos)
                position += pgap
                collected.append(position)
            positions = tuple(collected)
        yield Posting(doc_ord=doc_ord, tf=tf, positions=positions)


def encode_norms(lengths: Iterable[int]) -> bytes:
    """Encode per-document field lengths as one clamped byte each.

    Args:
        lengths: Field length (token count) for each document ordinal in order.

    Returns:
        One byte per document, values above :data:`_NORM_MAX` clamped down to it.
    """
    return bytes(min(max(length, 0), _NORM_MAX) for length in lengths)


def decode_norms(blob: bytes) -> list[int]:
    """Decode a norms blob produced by :func:`encode_norms`.

    Args:
        blob: A blob produced by :func:`encode_norms`.

    Returns:
        One integer per document ordinal, in order.
    """
    return list(blob)
