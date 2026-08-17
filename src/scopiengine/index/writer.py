"""``IndexWriter``: buffers an in-memory inverted index, flushing immutable segments.

Documents are stored durably (via
:meth:`~scopiengine.storage.base.StorageBackend.put_documents`) the moment they
are added, so :meth:`~scopiengine.index.manager.IndexManager.get_document` and id
lookups see them right away. Their *postings* stay in memory until
:meth:`IndexWriter.flush` — explicitly, or automatically once the buffer passes
:attr:`~scopiengine.settings.Settings.buffer_mb` — writes one immutable segment.
That segment is never mutated again: a later update is a delete of the old
ordinal (:meth:`~scopiengine.storage.base.StorageBackend.mark_deleted`) plus a
fresh append, and a merge (:mod:`scopiengine.index.segments`) only ever produces
a brand new segment, never edits one in place. This is what keeps "not visible
to search until refresh" a genuine, simple boundary rather than a partially-applied
mutation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.mapping.fieldtypes import terms_for_value
from scopiengine.mapping.mapping import FieldMapping, Mapping
from scopiengine.storage.base import SegmentTermEntry, StorageBackend
from scopiengine.storage.codec import encode_norms, encode_postings
from scopiengine.storage.models import NormsBlock, StoredDoc

__all__ = ["IndexWriter"]

#: Position gap inserted between successive elements of an array-valued text
#: field, so a phrase query can never accidentally span two different array
#: entries as if they were adjacent words in one piece of text.
_ARRAY_POSITION_GAP = 100

#: Rough per-posting byte cost used for the buffer-size estimate that triggers
#: an automatic flush — not exact (that would mean building the blob early),
#: just close enough that ``buffer_mb`` means roughly what it says.
_ESTIMATED_BYTES_PER_POSTING = 12


class IndexWriter:
    """Buffers postings for one index and flushes them as immutable segments.

    Args:
        storage: Backend the index lives in.
        index: Index name.
        buffer_mb: Buffer size, in megabytes, that triggers an automatic flush.
    """

    def __init__(self, storage: StorageBackend, index: str, *, buffer_mb: int) -> None:
        self.storage = storage
        self.index = index
        self.buffer_mb = buffer_mb
        self._postings: dict[tuple[int, str], dict[int, list[int]]] = {}
        self._lengths: dict[int, dict[int, int]] = {}
        self._with_positions: dict[int, bool] = {}
        self._min_ord: int | None = None
        self._max_ord: int | None = None
        self._buffer_bytes = 0
        self._field_ids: dict[str, int] = {}

    @property
    def buffered_doc_count(self) -> int:
        """Number of documents currently held in the unflushed buffer."""
        if self._min_ord is None or self._max_ord is None:
            return 0
        return self._max_ord - self._min_ord + 1

    def _resolve_field_ids(self, mapping: Mapping) -> None:
        missing = [name for name in mapping.fields if name not in self._field_ids]
        if missing:
            self._field_ids.update(self.storage.resolve_field_ids(self.index, missing))

    def add_documents(
        self,
        entries: Sequence[tuple[str, dict[str, Any], dict[str, Any]]],
        *,
        mapping: Mapping,
        analyzers: AnalyzerRegistry,
    ) -> list[int]:
        """Index and durably store a batch of documents.

        Args:
            entries: ``(doc_id, raw_document, flattened_document)`` triples —
                ``raw_document`` is stored verbatim as the retrievable source,
                ``flattened_document`` has every leaf value keyed by its dotted
                mapping path (matching :func:`scopiengine.mapping.dynamic.infer_additions`).
            mapping: The index's current, already-updated mapping. Every dotted
                path in ``flattened_document`` must be present in it.
            analyzers: Registry used to resolve each ``text`` field's analyzer.

        Returns:
            The document ordinal assigned to each entry, in order.

        Raises:
            IndexNotFoundError: The index does not exist.
        """
        if not entries:
            return []
        self._resolve_field_ids(mapping)

        base = self.storage.allocate_ords(self.index, len(entries))
        ords = list(range(base, base + len(entries)))
        self.storage.put_documents(
            self.index,
            [
                StoredDoc(doc_ord=ord_, doc_id=doc_id, source=json.dumps(raw, default=str).encode())
                for ord_, (doc_id, raw, _flat) in zip(ords, entries, strict=True)
            ],
        )

        if self._min_ord is None:
            self._min_ord = base
        self._max_ord = ords[-1]

        for doc_ord, (_doc_id, _raw, flat) in zip(ords, entries, strict=True):
            for path, value in flat.items():
                if value is None:
                    continue
                field_mapping = mapping.fields.get(path)
                if field_mapping is not None and field_mapping.index:
                    field_id = self._field_ids[path]
                    if field_mapping.type == "text":
                        self._index_text(field_id, doc_ord, value, field_mapping, analyzers)
                    else:
                        self._index_exact(field_id, doc_ord, value, field_mapping)

                # Dynamic mapping (see scopiengine.mapping.dynamic) adds a
                # "<path>.keyword" companion for every string field, driven by
                # the same JSON value rather than a distinct document key — so
                # it is indexed here, alongside the primary field, not looked
                # up separately in `flat`.
                companion = mapping.fields.get(f"{path}.keyword")
                if companion is not None and companion.index and companion.type == "keyword":
                    self._index_exact(self._field_ids[f"{path}.keyword"], doc_ord, value, companion)

        if self.buffered_bytes >= self.buffer_mb * 1024 * 1024:
            self.flush()
        return ords

    @property
    def buffered_bytes(self) -> int:
        """Rough estimate of the unflushed buffer's size, in bytes."""
        return self._buffer_bytes

    def _index_text(
        self,
        field_id: int,
        doc_ord: int,
        value: Any,
        field_mapping: FieldMapping,
        analyzers: AnalyzerRegistry,
    ) -> None:
        analyzer = analyzers.analyzer(field_mapping.analyzer or "standard")
        self._with_positions[field_id] = field_mapping.index_options == "positions"
        values = value if isinstance(value, list) else [value]
        length = 0
        offset = 0
        for item in values:
            if item is None:
                continue
            text = item if isinstance(item, str) else str(item)
            max_position = -1
            for term, position in analyzer.analyze(text):
                abs_position = offset + position
                self._postings.setdefault((field_id, term), {}).setdefault(doc_ord, []).append(
                    abs_position
                )
                self._buffer_bytes += _ESTIMATED_BYTES_PER_POSTING + len(term)
                length += 1
                max_position = max(max_position, position)
            offset += max_position + 1 + _ARRAY_POSITION_GAP
        if length:
            # doc_ord is unique within this call, so this is a first write, not
            # an accumulation — the addition guards only against a future caller
            # ever indexing the same (field, doc) pair twice in one batch.
            self._lengths.setdefault(field_id, {})[doc_ord] = (
                self._lengths.get(field_id, {}).get(doc_ord, 0) + length
            )

    def _index_exact(
        self, field_id: int, doc_ord: int, value: Any, field_mapping: FieldMapping
    ) -> None:
        self._with_positions[field_id] = False
        terms = terms_for_value(field_mapping.type, value, ignore_above=field_mapping.ignore_above)
        for term in terms:
            occurrences = self._postings.setdefault((field_id, term), {}).setdefault(doc_ord, [])
            occurrences.append(len(occurrences))
            self._buffer_bytes += _ESTIMATED_BYTES_PER_POSTING + len(term)
        if terms:
            self._lengths.setdefault(field_id, {})[doc_ord] = self._lengths.get(field_id, {}).get(
                doc_ord, 0
            ) + len(terms)

    def flush(self) -> int | None:
        """Write everything buffered as one new immutable segment.

        Returns:
            The new segment's id, or ``None`` when the buffer was empty.
        """
        if self._min_ord is None:
            return None
        assert self._max_ord is not None
        base_ord = self._min_ord
        doc_count = self._max_ord - base_ord + 1

        segment_id = self.storage.allocate_segment(self.index)

        def _postings_iter() -> Iterator[SegmentTermEntry]:
            for (field_id, term), by_doc in self._postings.items():
                with_positions = self._with_positions.get(field_id, False)
                rel_entries = sorted(
                    (
                        doc_ord - base_ord,
                        len(positions),
                        tuple(sorted(positions)) if with_positions else (),
                    )
                    for doc_ord, positions in by_doc.items()
                )
                blob = encode_postings(rel_entries, with_positions=with_positions)
                doc_freq = len(rel_entries)
                total_tf = sum(tf for _rel, tf, _positions in rel_entries)
                entry: SegmentTermEntry = (field_id, term, blob, doc_freq, total_tf)
                yield entry

        norms = []
        for field_id, by_doc in self._lengths.items():
            lengths = [0] * doc_count
            total_length = 0
            for doc_ord, length in by_doc.items():
                lengths[doc_ord - base_ord] = length
                total_length += length
            norms.append(
                NormsBlock(
                    field_id=field_id,
                    segment_id=segment_id,
                    base_ord=base_ord,
                    doc_count=doc_count,
                    total_length=total_length,
                    norms=encode_norms(lengths),
                )
            )

        self.storage.write_segment(
            self.index, segment_id, base_ord, doc_count, _postings_iter(), norms
        )
        self._reset_buffer()
        return segment_id

    def _reset_buffer(self) -> None:
        self._postings = {}
        self._lengths = {}
        self._with_positions = {}
        self._min_ord = None
        self._max_ord = None
        self._buffer_bytes = 0
