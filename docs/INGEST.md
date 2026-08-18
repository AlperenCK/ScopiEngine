# Log ingestion

`scopi ingest file` streams a log file into an index without ever holding the
whole thing in memory, checkpoints its progress as it goes, and resumes
exactly where it left off after a crash, a `kill -9`, or a clean Ctrl-C. This
document covers the pipeline, the guarantee the whole design exists to make
true, checkpoint/resume/rotation semantics, tuning, honest throughput
numbers, and troubleshooting.

## The pipeline

```
LocalFileSource -> ChunkedByteReader -> RecordAssembler -> Queue(maxsize=queue_size)
  -> processor chain -> Batcher -> [flush] one storage transaction
```

- **`ChunkedByteReader`** (`scopiengine.ingest.reader`) reads the source in
  fixed-size chunks (`chunk_bytes`, default 1 MiB — never the whole file),
  splits on `\n`, and keeps only the trailing partial line buffered between
  reads. A line longer than `max_line_bytes` (default 1 MiB) is truncated —
  counted, never allowed to grow the buffer without bound. Every complete
  line's bytes are decoded in one `str.decode("utf-8", errors="replace")`
  call, so a multi-byte character split across a chunk boundary is always
  decoded whole — the boundary lands inside the raw byte buffer, never inside
  a decode call.
- **`RecordAssembler`** (`scopiengine.ingest.assembler`) turns lines into
  records: one line is one record by default; `--multiline-start REGEX` folds
  every line that does not match into the previous record (stack traces). A
  record's offset is its first line's offset. Critically, a still-open
  multiline group is **never** returned as a record until either a new
  start-line arrives or the source is confirmed exhausted (`close()`, called
  only at real EOF in one-shot mode, never on a follow-mode "nothing new
  right now" pause) — so a crash mid-stack-trace resumes at the last
  completed record, never mid-record.
- A bounded **`queue.Queue(maxsize=queue_size)`** sits between the reader
  thread and the consumer (indexing) thread/loop. This is the entire
  backpressure mechanism: once the queue is full, `put()` blocks and the
  reader stops pulling more bytes off disk. Combined with the reader's fixed
  chunk size and the assembler holding at most one open record, **peak memory
  has a hard ceiling independent of file size** — see
  [Bounded memory](#bounded-memory) below.
- The **processor chain** (`scopiengine.plugins.builtin.processors`, the
  `ingest_processor` hook) transforms each record's seed document
  (`{"message": <record text>}`) in order. `--processor` is repeatable and
  resolves by name against `engine.plugins.ingest_processors` — a
  third-party plugin's processor is looked up on exactly the same path.
- The **`Batcher`** (`scopiengine.ingest.batcher`) accumulates processed
  documents until one of three triggers fires: `batch_size` documents,
  `batch_bytes` of encoded document size, or `flush_interval` seconds since
  the last flush — whichever comes first. The time trigger is what keeps
  `--follow` mode's visible lag bounded even when traffic is too light to
  fill a batch. A record that a processor drops still advances the batch's
  pending checkpoint offset (it was read and consumed, even though it did not
  become a document) — otherwise a resume would read, and re-drop, it
  forever.

## The single-transaction guarantee

> The checkpoint update is issued inside the same backend transaction as the
> segment and document writes it accounts for.

This is the core correctness property of the whole feature, and it is made
**structural**, not merely intended. `Checkpoint` lives in the same storage
backend as the documents and segments it accounts for (see
`storage/base.py`), and every backend's `transaction()` is **reentrant** on
its one connection: a `transaction()` block opened while already inside one
simply joins the outer transaction instead of committing early. So one batch
flush (`IngestPipeline._flush`) is exactly:

```python
with self.storage.transaction():
    if payload.docs:
        self.engine.index_documents(index, payload.docs, ids=payload.ids)  # put_documents
        self.engine.flush(index)                                          # write_segment
    self.storage.save_checkpoint(checkpoint)                              # advanced byte_offset
```

`index_documents` and `flush` each open their own `transaction()` internally
(that is how they behave correctly when called on their own, outside
ingestion); here they simply nest inside the outer one. Every write in that
block commits together, or none of them do:

- **A crash before the block returns** (a `SIGKILL` at any point up to and
  including partway through the `COMMIT`, since SQLite's WAL either applies a
  fully-written commit record or ignores an incomplete one on next open)
  leaves the backend exactly as it was before the block started: none of that
  batch's documents, no new segment, and the checkpoint still at its
  *previous* value. On resume, those bytes are read again from scratch.
- **A crash after it returns** leaves all three durably in place. On resume,
  reading continues from `byte_offset`, which is exactly the point up to
  which everything is durable.

There is no third state, and therefore no partial-batch reconciliation logic
anywhere in this codebase — an "was this batch half-applied?" check on
startup would only ever find the answer "no" by construction, so it was never
written.

`scopi index merge`/`scopi index refresh`'s auto-merge is **deliberately not**
called from the hot per-batch path — see [Tuning](#tuning) for why, and run
one of those commands after a big ingest for query performance.

## Checkpoints and resume

A checkpoint's key is `cp_key = blake2b(f"{index}|{abspath}").hexdigest()[:16]`
— stable across restarts, distinct per (index, absolute path) pair. Its
signature is `source_sig = f"{st_dev}:{st_ino}:{sha1(first 4 KiB)}"` **once the
file has reached 4 KiB** — used to tell "still the same file" apart from
"rotated out from under me" without reading the whole file on every check.
Below 4 KiB the content hash is left out and the signature is device and
inode alone: hashing "everything there is so far" for a file still growing
past that threshold would make the signature change on every single append,
which is indistinguishable from a rotation on every poll. Device and inode
are already stable across an in-place append, so they carry the signal alone
until there is a fixed window of content worth hashing.

On startup (or default, when nothing overrides it — see `--from` below),
resume compares the saved checkpoint against the source's *current* state:

| Condition | Outcome |
|---|---|
| No checkpoint on record | Start fresh at byte 0. |
| Signature matches, current size ≥ saved offset | **Resume**: seek to `byte_offset`, continue. |
| Signature matches, current size < saved offset | **Truncated in place** (a copytruncate rotation) — warn, restart at byte 0 with the same signature. |
| Signature differs | **Rotated or replaced** — warn, restart at byte 0 with the *new* signature. |

`--from start\|end\|checkpoint` overrides this outright: `start` always
begins at byte 0 (discarding any checkpoint), `end` seeks to the file's
current size (skip existing content, follow only what arrives from now on),
`checkpoint` is the table above. `--resume` is shorthand for `--from
checkpoint`; with neither flag, a run starts at byte 0 (see
[Duplicate protection](#duplicate-protection-and---id-mode) for why that is
still safe to do by accident).

### Rotation during `--follow`

The table above is the *startup* decision. While actively following a file,
each poll re-stats the source and applies the same signature/size comparison
against the currently-open handle's own last-known position. A **rotation**
(signature changed — a `rename` followed by a new file at the same path, or
an outright replacement) first **drains the old, already-open file handle to
EOF** — the underlying file descriptor is still valid even after the path it
was opened from now points elsewhere, so nothing written to the old file
before rotation is lost — flushes any trailing multiline record from it, and
only then opens the new file at byte 0 under its new signature. A
**truncation** (same signature, smaller size) closes and reopens the same
path directly at byte 0, since there is no old-file tail to drain.

### Duplicate protection and `--id-mode`

```
--id-mode offset    (default)  "{short_sig(source_sig)}:{byte_offset}"
--id-mode content                blake2b(record_text)
--id-mode uuid                   uuid4().hex, a fresh id every time
```

`offset` mode derives a document's external id from its byte position in a
signature-scoped id space, so **replaying the same bytes overwrites the same
document** — running `scopi ingest file x.log --index logs` twice in a row
with no `--resume` (the default) is a no-op the second time, not a
duplication. `content` mode hashes the record body instead, so the same line
shipped from a different path (or a different source entirely) still
deduplicates. `uuid` opts out of all of this deliberately — every record
becomes a new, independent document, useful when the source is expected to
be genuinely append-only and distinct documents per replayed record are
wanted.

Note that the single-transaction guarantee above already makes duplication
during a **resume** structurally impossible regardless of `--id-mode`: resume
continues from the exact byte offset the last commit accounted for, so there
is never any byte range read twice in the first place. `--id-mode offset`'s
overwrite behaviour is what makes an *accidental* full re-run (not a resume)
safe too.

## Tuning

| Setting | CLI flag | Default | Effect |
|---|---|---|---|
| `batch_size` | `--batch-size` | 5000 | Flush after this many documents. |
| `batch_bytes` | `--batch-bytes` | 8 MiB | Flush after this much encoded document size. |
| `flush_interval` | `--flush-interval` | 2s | Flush after this long regardless — bounds `--follow` lag. |
| `queue_size` | `--queue` | 8 | Reader/indexer queue depth — the backpressure knob and the main memory-vs-throughput trade-off. |
| `buffer_mb` | `--buffer-mb` | 64 | `IndexWriter`'s own in-memory postings buffer before an *extra* mid-batch segment flush. Rarely the binding constraint here, since a batch flush already happens on its own schedule. |

Raising `queue_size` and `batch_size`/`batch_bytes` trades memory for fewer,
larger transactions (better throughput, higher peak RSS); lowering them
trades throughput for a tighter memory ceiling. `flush_interval` is purely a
latency knob for `--follow` — lower it for near-real-time visibility, raise
it if many small transactions are the bottleneck.

Segments accumulate one per flushed batch during ingestion — `refresh`'s
auto-merge check is deliberately **not** run after every batch (it would turn
an O(batches) ingest into an O(batches²) one, since a merge rewrites the
entire live segment set every time it fires). Run `scopi index merge` (or
`scopi index refresh`, which merges once if `max_segments` is exceeded) after
a large ingest to fold the accumulated segments back down for query speed.

## Throughput, honestly

Measured on this development machine (single core, SQLite backend, default
settings, no merge during ingest — see above):

- **~14,500 docs/sec** for short, structured records (a handful of keyword/long
  fields plus a short text field) via `--processor json_line`.
- **~7,800 docs/sec** for records with one long free-text field indexed with
  the standard analyzer and position tracking — full-text analysis (not the
  ingest pipeline itself) is the dominant cost here; see
  `scopiengine.index.writer` for where that time goes.

Both numbers are throughput *into a single index on a single process*; they
say nothing about MS SQL Server's throughput under concurrent load, which
this document does not claim to have measured. Do not treat either figure as
a guarantee — measure on the target hardware and storage backend before
sizing a real deployment.

## Bounded memory

`tests/unit/test_ingest_bounded_memory.py` (marked `slow`, deselected by the
project's default `pytest -m "not slow"` CI run) ingests two files — 5 MiB and
60 MiB, an 11x size difference — through a real `scopi ingest file`
subprocess and samples the kernel's own peak-RSS counter
(`/proc/<pid>/status`'s `VmHWM`) throughout. Both runs stay under a ~340 MiB
ceiling, and peak RSS grows by well under 60 MiB between them — the interpreter
and SQLite driver's own fixed overhead dominates, not the file. That is what
this PR proves in CI: the property holds at a size increase large enough to
show file-size-proportional growth if it existed, in well under a minute.

**What this does not prove**, stated honestly: it has not been run against a
multi-gigabyte file in this environment. Nothing in the design is
size-dependent — the reader's buffer is capped independent of file size, the
queue is capped at `queue_size` records, and a batch is capped at
`batch_size`/`batch_bytes` documents — but a >1 GiB, real-world validation
run is a manual/operational step for whoever deploys this, not something
this test suite claims to have exercised.

## Troubleshooting

- **`ingest_error: unknown ingest processor 'x'; available: ...`** — the name
  passed to `--processor` is not registered. Run `scopi plugin list` to see
  what loaded; a third-party processor needs its plugin package installed
  and discoverable (entry point, or `SCOPI_PLUGINS`).
- **A rerun re-indexed everything from scratch** — `--resume` (or `--from
  checkpoint`) was not passed. The default `--from start` always begins at
  byte 0; this is safe (not a duplication) under the default `--id-mode
  offset`, but it does mean a full re-read.
- **"restarted at byte 0" warning on a file that was not touched** — before a
  file has grown past the 4 KiB signature window, its signature is derived
  from device and inode alone, precisely so an ordinary append cannot look
  like a rotation. If this warning still fires on an untouched file, the file
  has genuinely been replaced or its inode has been reused — check what wrote
  to that path.
- **`scopi ingest status` shows `state: failed`** — the reader hit an
  unrecoverable error (the source vanished mid-run without ever coming back,
  a permissions change, disk error). The `error` column carries the message.
  Everything up to the last successful batch is durable and searchable;
  fixing the underlying issue and re-running with `--resume` continues from
  there.
- **Query results look stale right after ingestion finishes** — each batch
  flush already calls the equivalent of a segment flush (`engine.flush`), so
  documents are searchable as soon as their batch commits, not only at the
  very end of the run; there is no separate "refresh" step needed. If a
  `scopi serve` process was already running before the ingest CLI process
  ran, restart or re-query it — the CLI and the server are separate
  processes over the same storage, and a long-lived server does not
  automatically notice another process's writes beyond what the storage
  backend itself makes visible on the next query.
- **Ingest seems slow** — see [Throughput, honestly](#throughput-honestly)
  above; full-text analysis of large free-text fields is the usual cost
  center, not the pipeline. Consider a `keyword`-typed mapping for fields
  that do not need tokenised search, or a coarser analyzer.

## CLI reference

```bash
scopi ingest file <path> --index logs
    [--follow]                       # keep polling for appended data
    [--resume]                       # shorthand for --from checkpoint
    [--from start|end|checkpoint]    # overrides --resume when given
    [--processor NAME]               # repeatable, applied in order
    [--multiline-start REGEX]        # folds continuation lines
    [--batch-size N] [--batch-bytes N] [--flush-interval SECONDS]
    [--buffer-mb N] [--queue N]
    [--id-mode offset|content|uuid]
    [--regex-pattern PATTERN]        # for the regex_extract processor
    [--no-progress]                  # suppress periodic stderr progress lines

scopi ingest status [--index NAME]   # list checkpoints, newest first
scopi ingest reset <cp_key>          # delete a checkpoint - next run starts at byte 0
```

Progress lines (stderr, unless `--no-progress`) report bytes/sec, docs/sec,
queue depth, live segment count and an ETA derived from the source's current
size. `Ctrl-C` (`SIGINT`) and `SIGTERM` both trigger a clean stop: the reader
stops pulling new bytes, the queue drains, the in-flight batch and its
checkpoint flush as one transaction, and the process exits **0** — a `--resume`
afterward continues exactly where it stopped, not from scratch.

## REST API reference

| Method & path | Purpose |
|---|---|
| `POST /_ingest/file` | Start a background ingest job. Body: `{"path", "index", "follow", "resume", "from", "processors": [...], "multiline_start", "id_mode", "batch_size", "batch_bytes", "flush_interval", "queue_size", "regex_pattern"}` — only `path` and `index` are required. Returns `202` with the job's initial status. |
| `GET /_ingest/jobs` | List every job started in this server process. |
| `GET /_ingest/jobs/{id}` | One job's live status (same shape `POST` returns). `404` for an unknown id. |
| `DELETE /_ingest/jobs/{id}` | Request a clean stop — the same drain-flush-checkpoint sequence `SIGINT` triggers on the CLI, not an abrupt cancel. `404` for an unknown id. |

Each job runs on its own background thread inside the server process, against
the same `Engine` every other endpoint uses — a job's documents are
searchable through `_search` as soon as their batches commit, the same as any
other write.
