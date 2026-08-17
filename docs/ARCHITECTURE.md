# Architecture

This document covers the layer PR 3 added on top of storage: analysis, mapping,
segments, scoring and the search path. For the storage layer itself (postings
codec, backend interface, DSNs) see [STORAGE_BACKENDS.md](STORAGE_BACKENDS.md).

```
                    Engine
        (create/delete index, index_documents,
         get/delete_document, refresh, force_merge,
              search, analyze, plugins)
                       |
          +------------+------------+
          |                         |
    IndexManager              AnalyzerRegistry
    (lifecycle, ingest,        + PluginRegistry
     refresh/merge, search)
          |
    +-----+-----+-----------------+
    |           |                 |
IndexWriter  IndexReader/Searcher  segments.force_merge
(buffer,     (per-query snapshot,  (rewrite live segments
 flush)       AST execution,       into one, purging
              BM25 scoring)        tombstones)
          |
    StorageBackend  (sqlite:// / mssql:// / mongodb:// / plugin-registered)
```

`Engine` (`src/scopiengine/engine.py`) is the one object the REST API and the
CLI both build on. It owns an `IndexManager`, an `AnalyzerRegistry` and a
`PluginRegistry`, and every public method is a thin pass-through to one of
them, firing an `event` hook where relevant.

## Segments and postings

An index's live content is split into **immutable segments**
(`src/scopiengine/index/segments.py`, `src/scopiengine/index/writer.py`).
`IndexWriter` buffers newly indexed documents' postings and field-length norms
in memory; `IndexManager.refresh()` flushes that buffer as one new segment via
`StorageBackend.write_segment()`. A segment, once written, is never edited —
not by an update, not by a delete:

- **Update** = mark the document's old ordinal deleted
  (`StorageBackend.mark_deleted`) + index the new content as a fresh document
  with a new ordinal. Ordinals are never reused (`allocate_ords` only ever
  advances), which is what makes the delete-and-append pattern safe: a
  tombstoned ordinal can never later collide with a live one.
- **Delete** is *soft*: an ordinal enters the `deletions` table and is filtered
  out at search time (`IndexReader.deleted`), but its postings stay physically
  present in their segment until the next merge.
- **Merge** (`segments.force_merge`, run explicitly via
  `IndexManager.force_merge`, or automatically once the live segment count
  exceeds `Settings.max_segments`) rewrites every live segment into one new
  segment, decoding each term's postings across every source segment,
  **dropping tombstoned ordinals**, and re-encoding. It is the only place
  tombstoned data actually leaves the postings/norms tables — the `deletions`
  and `documents` rows themselves are left alone, since a merge only owns
  segments and the tables segments write to. Document ordinals are **never
  renumbered** by a merge: `documents`, `deletions` and id resolution are all
  keyed by absolute ordinal independent of which segment currently covers it,
  so a merge just needs to span
  `[min(base_ord), max(base_ord + doc_count))` across the segments it merges.

Postings themselves are encoded by the existing, backend-agnostic
`scopiengine.storage.codec` (`encode_postings`/`decode_postings`,
`encode_norms`/`decode_norms`) — this layer reuses it rather than inventing a
second format. See STORAGE_BACKENDS.md for the byte layout.

### Buffering and flush

`IndexWriter` keeps three in-memory structures per index while documents are
being added: postings (`(field_id, term) -> {doc_ord: [positions]}`),
field-length norms (`field_id -> {doc_ord: length}`), and a rough byte-size
estimate. `IndexManager.index_documents()` calls `StorageBackend.put_documents()`
immediately — so a document is durable and fetchable by id right away — but its
postings stay buffered, invisible to search, until `refresh()` (explicit, or
automatic once the buffer estimate passes `Settings.buffer_mb`). This
"refresh to search" boundary matches how Lucene-family engines behave, and is
what makes "N segments vs. one force-merged segment return identical hits and
scores" a real invariant rather than an accident: nothing about scoring reads
buffer state, only flushed, refreshed segments.

## Order-preserving term encoding

`scopiengine.mapping.encoding` is the trick that lets `long`, `double` and
`date` fields share the exact same `terms` B-tree — and the same
`iter_terms(lo=..., hi=...)` range-scan primitive — as `text` and `keyword`
fields, instead of needing a second, numeric-aware storage path:

- **`long`**: shift into the unsigned 64-bit range and hex-pack —
  `format(v + 2**63, "016x")`. The bias turns two's-complement ordering (where
  a negative number's top bit is set, so raw integer comparison would put it
  *after* positives) into a plain unsigned range, which sorts correctly as
  fixed-width hex text.
- **`double`**: reinterpret the IEEE-754 bytes as an unsigned 64-bit integer,
  then flip bits — flip only the sign bit for a non-negative value (so it
  sorts above every negative encoding), flip every bit for a negative value
  (so a larger magnitude, i.e. more negative, yields a *smaller* unsigned
  integer). `+0.0` and `-0.0` are explicitly normalised to the same encoding
  before this transform, since they compare equal but would otherwise encode
  to different bit patterns. NaN has no defined position and is rejected.
- **`date`**: parsed from ISO-8601 to epoch milliseconds (UTC), then encoded
  via the `long` path — a date is an integer once you count milliseconds.

The invariant every one of these must hold — `encode(a) < encode(b) ⟺ a < b`,
across negatives, zero, the extremes of the range, and (for doubles) the
infinities — is checked in `tests/unit/test_mapping_encoding.py` against a
large seeded random sample (no `hypothesis` dependency is available in this
environment, so the property is checked directly rather than through a
shrinking property-testing library).

`Range` query bounds reuse this same trick from the other direction: an
exclusive bound (`gt`/`lt`) is turned into the inclusive form
`iter_terms(lo=, hi=)` expects by appending `"\0"` (the smallest possible code
point) — `gt` becomes `lo = gt + "\0"`, the tightest string strictly greater
than `gt` under ordinary lexicographic comparison, and correspondingly for
`lt`/`lte`. No post-filtering scan is needed; the bound passed to `iter_terms`
is already exact.

## Dynamic mapping

A field not yet in the mapping is inferred from the first document that uses
it (`scopiengine.mapping.dynamic`): a JSON string becomes `text` **and** gets a
`.keyword` sibling field (`ignore_above: 256`) — two dotted mapping paths from
one JSON key, the same flattening trick `object` mapping already uses for
nesting. That sibling is what makes aggregating or sorting on a log field work
without anyone writing a mapping by hand: `message` is analyzed and searchable,
`message.keyword` is an exact-match term capped at 256 characters.

A field already present in the mapping — however it got there, explicit or a
previous document's dynamic addition — is never reinterpreted by a later
document. This matters more than it might look: without it, an explicitly
mapped `level: keyword` field would collide with dynamic inference's default
guess of `text` for the very same string value on every single document
indexed. A genuine type conflict between two *new* fields introduced by the
same document (nonsensical shapes like an `object` and a scalar at the same
path) still raises `MappingError`; a conflict against an *already-mapped*
field surfaces later, from `scopiengine.mapping.fieldtypes`, when the
mismatched value actually tries to encode against that field's real type.

## Scoring

BM25 (`scopiengine.index.scoring`), with the conventional `k1 = 1.2`,
`b = 0.75`:

```
idf(N, df)   = ln(1 + (N - df + 0.5) / (df + 0.5))
score(tf,dl) = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
```

`N` is the index's live document count. `avgdl` is a field's average length,
aggregated across every live segment. Both `df` (a term's document frequency)
and `avgdl` are recomputed **from the live postings and norms themselves**
at search time — deliberately not trusted from the raw, stored
`total_length`/`doc_count`/`doc_freq` numbers a segment was written with,
because those numbers reflect the state of the world at *write* time and never
get updated by a later delete. Recomputing them from what is actually live
right now is what makes the force-merge invariant hold: a term's `df`, and a
field's `avgdl`, must come out identical whether the matching documents
currently live in one segment or five, and the only way to guarantee that is
to derive both from the same "is this ordinal currently live" check
(`IndexReader.deleted`) a merge itself uses to decide what to drop.

## The search path

`scopiengine.query.ast` defines the query AST PR 4's parsers will build:
`MatchAll | Term | Phrase | Prefix | Range | Exists | Bool(must, should,
must_not, filter)`, all frozen dataclasses. `scopiengine.index.searcher`
executes it against an `IndexReader` — a cached, read-only snapshot built once
per search call (field ids, segment list, deleted-ordinal set, per-segment
norms are all resolved once and reused across every clause in the query).

Every leaf and combinator is a generator yielding `(doc_ord, score)` pairs in
ascending `doc_ord` order, so they compose via streaming merges instead of ever
materialising a full result set:

- **`Term`**: `heapq.merge`s a term's per-segment posting streams (each
  produced by lazily decoding that segment's blob via
  `storage.codec.decode_postings`), filtering deleted ordinals and scoring each
  live posting with BM25. (Computing `df` up front does mean this term's own
  live postings are decoded in full before any score is emitted — bounded by
  that one term's document frequency, not the corpus, and unavoidable since
  BM25's idf needs the final live count before it can score anything.)
- **`Phrase`**: leapfrog-intersects every term's posting stream by document
  (`_leapfrog`, a generic multi-way merge-join), then checks position
  alignment within each shared document. Requires the field to be mapped with
  `index_options="positions"` — anything else raises
  `UnsupportedFeatureError` rather than silently returning wrong (unordered)
  matches. Slop is handled by a greedy, in-order nearest-position match; it is
  not an exhaustive search over every possible alignment, and says so in its
  docstring.
- **`Prefix`** / **`Range`**: an ordered term scan
  (`IndexReader.iter_terms(prefix=...)` / `(lo=..., hi=...)`) turned into a
  union of `Term`-style matchers, scores summed like an implicit multi-term
  "should".
- **`Exists`**: a union across every term in the field, deduplicated to a
  constant score of `1.0` — existence, not relevance, is what this node means.
  (This is the one leaf without a bounded cost: a field with a very large
  vocabulary means a stream per distinct term. There is no dedicated
  "has-a-value" bitset in this release.)
- **`Bool`**: `must`/`filter` leapfrog-intersect (`_merge_and`) — filter
  clauses are score-zeroed first, so they gate without contributing; `should`
  either unions the result (`_merge_or`, when there is no `must`/`filter`) or
  optionally boosts an already-gated result (`_boost_should`, a synchronized
  lookahead against each `should` stream); `must_not` is a streaming anti-join
  (`_merge_exclude`) against whatever the gate ends up being. An empty `Bool()`
  matches everything, same as `MatchAll`.

The final top-N cut (`search()`) keeps a single bounded heap of size
`size + from_` and never grows past it, regardless of how many documents the
query matches — so the memory a search call uses is governed by how many
results are kept, not by corpus size. Hits are ordered by descending score,
ties broken by ascending ordinal; that tie-break depends only on each match's
own score and ordinal, never on segment layout, which is what lets the
force-merge invariant assert exact equality of the *ordered* hit list, not
just the matched set.

## Deletes, updates and the `documents` table

Soft deletes are cheap and simple for the inverted index (`deletions` is one
small ordinal set, checked once per search), but the `documents` table isn't
free to reason about the same way: it enforces one external id per row
(`UNIQUE(index_id, doc_id)`), and tombstoning an ordinal does not touch that
row. Re-indexing an existing id under "delete old ordinal, append new" would
therefore collide — the new row wants the same external id the old, still
physically-present row already holds. `IndexManager._retire_existing`
resolves this without touching the storage schema: before writing the
replacement, it renames the old ordinal's row to an unreachable placeholder id
(freeing the real id), then tombstones the old ordinal. This is the one
private implementation detail worth calling out explicitly, since it is not
obvious from the public `index_documents` signature that a same-id write does
more than one storage round trip.

## What is explicitly out of scope here

- **Query parsers** (ScopiQL, JSON-DSL): PR 4. Only the AST exists so far.
- **Stemming**: ships as the reference plugin in 1.1 — the whole reason
  `AnalyzerRegistry` is a mutable registry rather than a fixed enum.
- **Remote ingest sources**: `scopiengine.ingest.sources.Source` is a narrow
  protocol (`open`/`stat`) with `LocalFileSource` as the only implementation;
  SFTP/SSH/syslog/S3 sources are the seam this leaves for 1.1, not built here.
- **A dedicated exists/field-cardinality index**: `Exists` works by unioning
  every term in a field, which does not scale to very high-cardinality fields.
- **Per-field `store`**: `FieldMapping.store` is a recognised setting but does
  not yet change retrieval behaviour beyond the document's full stored source,
  which is always retrievable regardless.
