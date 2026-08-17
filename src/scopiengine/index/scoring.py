"""BM25 relevance scoring.

Okapi BM25 with the conventional ``k1=1.2``, ``b=0.75``::

    idf(N, df)             = ln(1 + (N - df + 0.5) / (df + 0.5))
    score(tf, dl, avgdl)   = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

``N`` is the number of live documents in the index (from
:meth:`~scopiengine.storage.base.StorageBackend.index_stats`); ``df`` is a term's
document frequency (from
:meth:`~scopiengine.storage.base.StorageBackend.get_term_stats`); ``dl`` is a
document's length in the scored field (tokens indexed, from
:class:`~scopiengine.storage.models.NormsBlock`); ``avgdl`` is that field's
average length across every live segment, aggregated in
:meth:`scopiengine.index.reader.IndexReader.avg_field_length`.
"""

from __future__ import annotations

import math

__all__ = ["K1", "B", "bm25_score", "idf"]

#: Term frequency saturation. Higher values let repeated occurrences of a term
#: keep contributing to the score for longer before diminishing returns kick in.
K1 = 1.2

#: Document length normalisation strength: ``0`` ignores length entirely, ``1``
#: fully normalises by it.
B = 0.75


def idf(n_docs: int, doc_freq: int) -> float:
    """Inverse document frequency for a term appearing in ``doc_freq`` of ``n_docs`` documents."""
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def bm25_score(
    *,
    tf: int,
    doc_length: int,
    avg_doc_length: float,
    doc_freq: int,
    n_docs: int,
) -> float:
    """Compute one term's BM25 contribution to one document's score.

    Args:
        tf: The term's frequency in this document's field.
        doc_length: This document's length (token count) in the scored field.
        avg_doc_length: The field's average length across the index.
        doc_freq: Number of documents the term occurs in.
        n_docs: Number of live documents in the index.

    Returns:
        The BM25 score contribution. ``0.0`` when ``n_docs`` or ``doc_freq`` is
        zero (nothing to score against).
    """
    if n_docs <= 0 or doc_freq <= 0 or tf <= 0:
        return 0.0
    length_norm = 1.0 - B + B * (doc_length / avg_doc_length if avg_doc_length > 0 else 0.0)
    denominator = tf + K1 * length_norm
    if denominator == 0:
        return 0.0
    return idf(n_docs, doc_freq) * (tf * (K1 + 1)) / denominator
