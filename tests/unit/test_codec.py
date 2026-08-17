"""The postings codec round-trips, decodes lazily, and rejects corrupt input."""

from __future__ import annotations

import itertools
import random

import pytest

from scopiengine.errors import StorageError
from scopiengine.storage.codec import decode_norms, decode_postings, encode_norms, encode_postings
from scopiengine.storage.models import Posting


def test_round_trip_without_positions() -> None:
    postings = [(0, 1, ()), (3, 2, ()), (10, 5, ()), (11, 1, ())]
    blob = encode_postings(postings, with_positions=False)
    decoded = list(decode_postings(blob))
    assert decoded == [Posting(doc_ord=o, tf=tf, positions=()) for o, tf, _ in postings]


def test_round_trip_with_positions() -> None:
    postings = [
        (0, 2, (1, 5)),
        (4, 1, (0,)),
        (9, 3, (2, 3, 40)),
    ]
    blob = encode_postings(postings, with_positions=True)
    decoded = list(decode_postings(blob))
    assert decoded == [Posting(doc_ord=o, tf=tf, positions=p) for o, tf, p in postings]


def test_empty_postings_round_trip() -> None:
    blob = encode_postings([], with_positions=True)
    assert list(decode_postings(blob)) == []


@pytest.mark.parametrize("with_positions", [False, True])
def test_randomised_postings_round_trip(with_positions: bool) -> None:
    rng = random.Random(1234)
    for _ in range(20):
        n_docs = rng.randint(0, 200)
        doc_ord = 0
        postings = []
        for _ in range(n_docs):
            doc_ord += rng.randint(1, 50)
            tf = rng.randint(1, 20)
            positions: tuple[int, ...] = ()
            if with_positions:
                position = 0
                collected = []
                for _p in range(tf):
                    position += rng.randint(1, 30)
                    collected.append(position)
                positions = tuple(collected)
            postings.append((doc_ord, tf, positions))
        blob = encode_postings(postings, with_positions=with_positions)
        decoded = list(decode_postings(blob))
        assert decoded == [Posting(doc_ord=o, tf=tf, positions=p) for o, tf, p in postings]


def _make_large_postings(n: int) -> list[tuple[int, int, tuple[int, ...]]]:
    return [(doc_ord, 1, ()) for doc_ord in range(n)]


def test_large_blob_round_trips() -> None:
    n = 1_000_000
    postings = _make_large_postings(n)
    blob = encode_postings(postings, with_positions=False)
    decoded = decode_postings(blob)
    count = 0
    for expected_ord, posting in zip(range(n), decoded, strict=True):
        assert posting.doc_ord == expected_ord
        count += 1
    assert count == n


def test_decode_postings_is_lazy() -> None:
    """Consuming only the first few postings must not decode the rest of the blob."""
    n = 1_000_000
    postings = _make_large_postings(n)
    blob = encode_postings(postings, with_positions=False)

    decoder = decode_postings(blob)
    first_five = list(itertools.islice(decoder, 5))
    assert [p.doc_ord for p in first_five] == [0, 1, 2, 3, 4]

    # decode_postings must be a generator: nothing beyond the requested items is
    # produced eagerly, so the generator object itself stays alive and paused
    # rather than having already materialised a 1,000,000-item list.
    import inspect

    assert inspect.isgenerator(decoder)
    # The generator can still be resumed and continues where it left off.
    sixth = next(decoder)
    assert sixth.doc_ord == 5


def test_encode_rejects_non_ascending_doc_ord() -> None:
    with pytest.raises(StorageError, match="ascending"):
        encode_postings([(5, 1, ()), (3, 1, ())], with_positions=False)


def test_encode_rejects_duplicate_doc_ord() -> None:
    with pytest.raises(StorageError, match="ascending"):
        encode_postings([(5, 1, ()), (5, 1, ())], with_positions=False)


def test_encode_rejects_non_ascending_positions() -> None:
    with pytest.raises(StorageError, match="ascending"):
        encode_postings([(0, 2, (5, 3))], with_positions=True)


def test_decode_rejects_truncated_blob() -> None:
    blob = encode_postings([(0, 1, ()), (2, 3, ())], with_positions=False)
    with pytest.raises(StorageError, match="truncated"):
        list(decode_postings(blob[:-1]))


def test_decode_rejects_empty_blob() -> None:
    with pytest.raises(StorageError, match="truncated"):
        list(decode_postings(b""))


def test_decode_rejects_bad_magic() -> None:
    blob = bytearray(encode_postings([(0, 1, ())], with_positions=False))
    blob[0] = 0xFF
    with pytest.raises(StorageError, match="magic"):
        list(decode_postings(bytes(blob)))


def test_norms_round_trip() -> None:
    lengths = [0, 1, 5, 254, 255]
    blob = encode_norms(lengths)
    assert decode_norms(blob) == lengths


def test_norms_clamp_at_255() -> None:
    blob = encode_norms([256, 1000, -5])
    assert decode_norms(blob) == [255, 255, 0]


def test_encode_rejects_position_count_disagreeing_with_tf() -> None:
    # The decoder reads exactly tf position gaps, so accepting a mismatch here
    # would silently desynchronise every posting after this one.
    with pytest.raises(StorageError, match="tf=3 but carries 2 positions"):
        encode_postings([(0, 3, (1, 2))], with_positions=True)


def test_position_count_is_not_checked_when_positions_are_not_encoded() -> None:
    blob = encode_postings([(0, 3, ())], with_positions=False)
    assert list(decode_postings(blob)) == [Posting(doc_ord=0, tf=3, positions=())]


def test_bad_header_is_reported_at_the_call_site_not_on_iteration() -> None:
    # Header validation is eager, so a misrouted blob fails where it was passed in.
    with pytest.raises(StorageError, match="magic"):
        decode_postings(b"\xff\x00\x00")
    with pytest.raises(StorageError, match="truncated"):
        decode_postings(b"")
