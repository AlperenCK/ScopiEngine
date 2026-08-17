"""``IndexReader``: a cached, read-only view of one index for one search call.

Every storage round trip a :class:`~scopiengine.index.searcher.Searcher` needs —
field ids, segment lists, term stats, norms, the deleted-ordinal set — goes
through here exactly once per search, rather than once per query clause. A
reader is built fresh for each :meth:`~scopiengine.index.manager.IndexManager.search`
call, so its caches never need invalidating: an index change is only visible to
the *next* reader, matching the segment model's own "immutable until refresh"
semantics.
"""

from __future__ import annotations

from collections.abc import Iterator

from scopiengine.mapping.mapping import Mapping
from scopiengine.storage.base import StorageBackend
from scopiengine.storage.codec import decode_norms
from scopiengine.storage.models import SegmentInfo, SegmentState, TermStats

__all__ = ["IndexReader"]


class IndexReader:
    """A read-only, cached snapshot of one index, as seen by one search call.

    Args:
        storage: The backend to read from.
        index: Index name.
        mapping: The index's current field mapping.
    """

    def __init__(self, storage: StorageBackend, index: str, mapping: Mapping) -> None:
        self.storage = storage
        self.index = index
        self.mapping = mapping
        self._field_ids: dict[str, int] = (
            storage.resolve_field_ids(index, list(mapping.fields)) if mapping.fields else {}
        )
        self._segments: list[SegmentInfo] | None = None
        self._deleted: frozenset[int] | None = None
        self._norms_cache: dict[tuple[int, int], tuple[list[int], int] | None] = {}
        self._avg_length_cache: dict[int, float] = {}
        self._live_doc_count: int | None = None

    # -- fields ------------------------------------------------------------

    def field_id(self, name: str) -> int | None:
        """The stable id for a mapped field, or ``None`` if ``name`` is not mapped.

        A query against an unmapped field is not an error — it simply matches
        nothing, the same as any other query with no matching terms.
        """
        return self._field_ids.get(name)

    # -- segments and deletions ---------------------------------------------

    @property
    def live_segments(self) -> list[SegmentInfo]:
        """Every segment in :data:`~scopiengine.storage.models.SegmentState.LIVE` state."""
        if self._segments is None:
            self._segments = [
                s for s in self.storage.list_segments(self.index) if s.state == SegmentState.LIVE
            ]
        return self._segments

    @property
    def deleted(self) -> frozenset[int]:
        """Every tombstoned document ordinal in the index."""
        if self._deleted is None:
            self._deleted = frozenset(self.storage.iter_deleted(self.index))
        return self._deleted

    @property
    def live_doc_count(self) -> int:
        """Number of live (non-deleted) documents — BM25's ``N``."""
        if self._live_doc_count is None:
            self._live_doc_count = int(self.storage.index_stats(self.index)["live_doc_count"])
        return self._live_doc_count

    def iter_live_ords(self) -> Iterator[int]:
        """Yield every live document ordinal, ascending, across every segment."""
        for segment in self.live_segments:
            for rel in range(segment.doc_count):
                abs_ord = segment.base_ord + rel
                if abs_ord not in self.deleted:
                    yield abs_ord

    # -- terms and postings ---------------------------------------------------

    def iter_terms(
        self,
        field_id: int,
        *,
        prefix: str | None = None,
        lo: str | None = None,
        hi: str | None = None,
    ) -> Iterator[str]:
        """See :meth:`scopiengine.storage.base.StorageBackend.iter_terms`."""
        return self.storage.iter_terms(self.index, field_id, prefix=prefix, lo=lo, hi=hi)

    def get_postings(self, field_id: int, term: str) -> list[tuple[int, int, bytes]]:
        """See :meth:`scopiengine.storage.base.StorageBackend.get_postings`."""
        return self.storage.get_postings(self.index, field_id, term)

    def term_stats(self, field_id: int, term: str) -> TermStats:
        """See :meth:`scopiengine.storage.base.StorageBackend.get_term_stats`."""
        return self.storage.get_term_stats(self.index, field_id, term)

    # -- norms and document length --------------------------------------------

    def _norms(self, field_id: int, segment_id: int) -> tuple[list[int], int] | None:
        """Fetch and cache one segment's decoded norms for one field."""
        key = (field_id, segment_id)
        if key not in self._norms_cache:
            block = self.storage.get_norms(self.index, field_id, segment_id)
            self._norms_cache[key] = (
                None if block is None else (decode_norms(block.norms), block.base_ord)
            )
        return self._norms_cache[key]

    def doc_length(self, field_id: int, segment_id: int, rel_ord: int) -> int:
        """A document's length (token count) in ``field_id``, or ``0`` if unrecorded."""
        found = self._norms(field_id, segment_id)
        if found is None:
            return 0
        lengths, _base_ord = found
        return lengths[rel_ord] if 0 <= rel_ord < len(lengths) else 0

    def avg_field_length(self, field_id: int) -> float:
        """The field's average document length across every *live* document — BM25's ``avgdl``.

        Deleted ordinals are excluded from both the sum and the count, computed
        by decoding each segment's norms and checking :attr:`deleted` per
        ordinal — not by trusting
        :attr:`~scopiengine.storage.models.NormsBlock.total_length` /
        ``doc_count`` directly, since those are written once at segment-flush
        time and never updated by a later delete. Excluding deleted ordinals
        here is what keeps this average identical before and after a merge:
        :mod:`scopiengine.index.segments` already zeroes deleted ordinals out
        of a merged segment's norms, so computing the average any other way
        (trusting the raw per-segment totals) would silently shift once a
        merge ran, even though nothing a query cares about changed.
        """
        if field_id not in self._avg_length_cache:
            total_length = 0
            total_docs = 0
            for segment in self.live_segments:
                found = self._norms(field_id, segment.segment_id)
                if found is not None:
                    lengths, _base_ord = found
                    for rel, length in enumerate(lengths):
                        if segment.base_ord + rel not in self.deleted:
                            total_length += length
                            total_docs += 1
            self._avg_length_cache[field_id] = (total_length / total_docs) if total_docs else 0.0
        return self._avg_length_cache[field_id]

    # -- documents -----------------------------------------------------------

    def get_document(self, doc_ord: int) -> bytes | None:
        """Fetch one live document's stored source, or ``None`` if absent or deleted."""
        if doc_ord in self.deleted:
            return None
        found = self.storage.get_documents(self.index, [doc_ord])
        return found[0].source if found else None
