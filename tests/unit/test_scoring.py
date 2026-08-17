"""BM25 scoring tests, checked against a hand-computed fixture.

Fixture: three documents, one field ("body"), no deletions.

======  ==  ======  ===============
doc     tf  length  contains "apple"
======  ==  ======  ===============
0        1       4  yes
1        3       6  yes
2        -       5  no
======  ==  ======  ===============

``N = 3`` (three live documents). ``df = 2`` ("apple" occurs in docs 0 and 1).
``avgdl = (4 + 6 + 5) / 3 = 5``.

BM25, with ``k1 = 1.2``, ``b = 0.75``::

    idf = ln(1 + (N - df + 0.5) / (df + 0.5))
        = ln(1 + (3 - 2 + 0.5) / (2 + 0.5))
        = ln(1 + 1.5 / 2.5)
        = ln(1.6)
        = 0.4700036292457356...

    score(tf, dl) = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

    score(doc0): tf=1, dl=4
        length_norm = 1 - 0.75 + 0.75 * 4/5 = 0.25 + 0.6 = 0.85
        denom       = 1 + 1.2 * 0.85 = 1 + 1.02 = 2.02
        numer       = 1 * (1.2 + 1) = 2.2
        score       = 0.4700036292457356 * 2.2 / 2.02 = 0.5118851407626824...

    score(doc1): tf=3, dl=6
        length_norm = 1 - 0.75 + 0.75 * 6/5 = 0.25 + 0.9 = 1.15
        denom       = 3 + 1.2 * 1.15 = 3 + 1.38 = 4.38
        numer       = 3 * (1.2 + 1) = 6.6
        score       = 0.4700036292457356 * 6.6 / 4.38 = 0.7082246468086428...

doc1's higher term frequency (3 vs 1) outweighs its longer-than-average length
(6 vs avgdl 5), so it outscores doc0 — the fixture is deliberately not
monotonic in either tf or length alone, so a scoring bug that drops either
term from the formula would still fail this test.
"""

from __future__ import annotations

import math

import pytest

from scopiengine.index.scoring import K1, B, bm25_score, idf

# -- fixture constants, matching the module docstring's worked arithmetic ----

_N_DOCS = 3
_DOC_FREQ = 2
_AVG_DOC_LENGTH = 5.0
_EXPECTED_IDF = 0.4700036292457356
_EXPECTED_SCORE_DOC0 = 0.5118851407626824
_EXPECTED_SCORE_DOC1 = 0.7082246468086428


def test_bm25_constants_match_the_spec() -> None:
    assert K1 == 1.2
    assert B == 0.75


def test_idf_matches_hand_computed_value() -> None:
    assert idf(_N_DOCS, _DOC_FREQ) == pytest.approx(_EXPECTED_IDF)
    # ln(1.6) directly, as a cross-check independent of the idf() call above.
    assert idf(_N_DOCS, _DOC_FREQ) == pytest.approx(math.log(1.6))


def test_bm25_score_doc0_matches_hand_computed_value() -> None:
    score = bm25_score(
        tf=1, doc_length=4, avg_doc_length=_AVG_DOC_LENGTH, doc_freq=_DOC_FREQ, n_docs=_N_DOCS
    )
    assert score == pytest.approx(_EXPECTED_SCORE_DOC0)


def test_bm25_score_doc1_matches_hand_computed_value() -> None:
    score = bm25_score(
        tf=3, doc_length=6, avg_doc_length=_AVG_DOC_LENGTH, doc_freq=_DOC_FREQ, n_docs=_N_DOCS
    )
    assert score == pytest.approx(_EXPECTED_SCORE_DOC1)


def test_higher_tf_outscores_lower_tf_despite_longer_document() -> None:
    score0 = bm25_score(
        tf=1, doc_length=4, avg_doc_length=_AVG_DOC_LENGTH, doc_freq=_DOC_FREQ, n_docs=_N_DOCS
    )
    score1 = bm25_score(
        tf=3, doc_length=6, avg_doc_length=_AVG_DOC_LENGTH, doc_freq=_DOC_FREQ, n_docs=_N_DOCS
    )
    assert score1 > score0


# -- edge cases --------------------------------------------------------------


def test_zero_doc_freq_scores_zero() -> None:
    assert bm25_score(tf=1, doc_length=4, avg_doc_length=5.0, doc_freq=0, n_docs=3) == 0.0


def test_zero_n_docs_scores_zero() -> None:
    assert bm25_score(tf=1, doc_length=4, avg_doc_length=5.0, doc_freq=1, n_docs=0) == 0.0


def test_zero_tf_scores_zero() -> None:
    assert bm25_score(tf=0, doc_length=4, avg_doc_length=5.0, doc_freq=1, n_docs=3) == 0.0


def test_zero_avg_doc_length_does_not_raise() -> None:
    # A field with no norms yet (avgdl computed from zero documents) must not
    # divide by zero — see IndexReader.avg_field_length's fallback to 0.0.
    score = bm25_score(tf=1, doc_length=0, avg_doc_length=0.0, doc_freq=1, n_docs=3)
    assert score >= 0.0


def test_a_rarer_term_scores_higher_all_else_equal() -> None:
    common = bm25_score(tf=2, doc_length=5, avg_doc_length=5.0, doc_freq=50, n_docs=100)
    rare = bm25_score(tf=2, doc_length=5, avg_doc_length=5.0, doc_freq=1, n_docs=100)
    assert rare > common
