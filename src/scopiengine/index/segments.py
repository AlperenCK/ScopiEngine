"""Segment merging: ``force_merge`` and the auto-merge threshold check.

A merge never renumbers document ordinals. Every live segment's absolute
ordinal range is already unique and permanent (assigned once, at
:meth:`~scopiengine.storage.base.StorageBackend.allocate_ords` time), and both
the ``documents`` table and the tombstone set in
:meth:`~scopiengine.storage.base.StorageBackend.iter_deleted` are keyed by that
same absolute ordinal, independent of which segment currently covers it. So a
merge just needs to produce one new segment spanning
``[min(base_ord), max(base_ord + doc_count))`` across the segments being
merged, with tombstoned ordinals dropped from its postings and norms — nothing
above the segment layer (document lookup, id resolution, delete tracking) has
to know a merge happened at all.
"""

from __future__ import annotations

from scopiengine.mapping.mapping import Mapping
from scopiengine.storage.base import SegmentTermEntry, StorageBackend
from scopiengine.storage.codec import decode_norms, decode_postings, encode_norms, encode_postings
from scopiengine.storage.models import NormsBlock, SegmentInfo, SegmentState

__all__ = ["force_merge", "should_auto_merge"]


def should_auto_merge(storage: StorageBackend, index: str, *, max_segments: int) -> bool:
    """Whether the live segment count exceeds ``max_segments``."""
    live = [s for s in storage.list_segments(index) if s.state == SegmentState.LIVE]
    return len(live) > max_segments


def _field_ids(storage: StorageBackend, index: str, mapping: Mapping) -> list[int]:
    if not mapping.fields:
        return []
    return sorted(storage.resolve_field_ids(index, list(mapping.fields)).values())


def _merged_postings(
    storage: StorageBackend,
    index: str,
    field_ids: list[int],
    new_base_ord: int,
    deleted: frozenset[int],
) -> list[SegmentTermEntry]:
    entries: list[SegmentTermEntry] = []
    for field_id in field_ids:
        for term in storage.iter_terms(index, field_id):
            combined: list[tuple[int, int, tuple[int, ...]]] = []
            with_positions = False
            for _segment_id, base_ord, blob in storage.get_postings(index, field_id, term):
                for posting in decode_postings(blob):
                    abs_ord = base_ord + posting.doc_ord
                    if abs_ord in deleted:
                        continue
                    if posting.positions:
                        with_positions = True
                    combined.append((abs_ord - new_base_ord, posting.tf, posting.positions))
            if not combined:
                continue
            combined.sort(key=lambda item: item[0])
            blob = encode_postings(
                (
                    (rel_ord, tf, positions if with_positions else ())
                    for rel_ord, tf, positions in combined
                ),
                with_positions=with_positions,
            )
            doc_freq = len(combined)
            total_tf = sum(tf for _rel, tf, _positions in combined)
            entries.append((field_id, term, blob, doc_freq, total_tf))
    return entries


def _merged_norms(
    storage: StorageBackend,
    index: str,
    field_ids: list[int],
    live_segments: list[SegmentInfo],
    new_segment_id: int,
    new_base_ord: int,
    new_doc_count: int,
    deleted: frozenset[int],
) -> list[NormsBlock]:
    blocks = []
    for field_id in field_ids:
        lengths = [0] * new_doc_count
        found_any = False
        for segment in live_segments:
            block = storage.get_norms(index, field_id, segment.segment_id)
            if block is None:
                continue
            found_any = True
            offset = segment.base_ord - new_base_ord
            for rel, length in enumerate(decode_norms(block.norms)):
                abs_ord = segment.base_ord + rel
                if abs_ord in deleted:
                    continue
                lengths[offset + rel] = length
        if found_any:
            blocks.append(
                NormsBlock(
                    field_id=field_id,
                    segment_id=new_segment_id,
                    base_ord=new_base_ord,
                    doc_count=new_doc_count,
                    total_length=sum(lengths),
                    norms=encode_norms(lengths),
                )
            )
    return blocks


def force_merge(storage: StorageBackend, index: str, mapping: Mapping) -> int | None:
    """Rewrite every live segment into a single new one, purging tombstoned documents.

    Deletion tombstones themselves (:meth:`~scopiengine.storage.base.StorageBackend.iter_deleted`)
    and stored document rows are left untouched — a merge only ever compacts
    postings and norms, never the ``documents``/``deletions`` tables the segment
    layer does not own.

    Returns:
        The new segment's id, or ``None`` when there was at most one live
        segment (nothing to merge).

    Raises:
        IndexNotFoundError: The index does not exist.
    """
    live_segments = [s for s in storage.list_segments(index) if s.state == SegmentState.LIVE]
    if len(live_segments) <= 1:
        return None

    new_base_ord = min(s.base_ord for s in live_segments)
    new_doc_count = max(s.base_ord + s.doc_count for s in live_segments) - new_base_ord
    deleted = frozenset(storage.iter_deleted(index))
    field_ids = _field_ids(storage, index, mapping)

    new_segment_id = storage.allocate_segment(index)
    postings = _merged_postings(storage, index, field_ids, new_base_ord, deleted)
    norms = _merged_norms(
        storage,
        index,
        field_ids,
        live_segments,
        new_segment_id,
        new_base_ord,
        new_doc_count,
        deleted,
    )
    storage.write_segment(index, new_segment_id, new_base_ord, new_doc_count, postings, norms)
    storage.drop_segments(index, [s.segment_id for s in live_segments])
    return new_segment_id
