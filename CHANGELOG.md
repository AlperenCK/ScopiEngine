# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **`--follow` on a file smaller than 4 KiB duplicated every appended line on
  every poll.** `compute_signature` hashed the file's entire current content
  whenever it was under the 4 KiB head-hash window, so a genuinely unchanged,
  still-growing file got a new signature on every append; `classify_change`
  cannot tell that apart from a real rotation, closed the handle, and
  reopened the file at byte 0. Because `--id-mode offset` (the default) bakes
  the signature into each document id, the reprocessed lines landed as brand
  new documents rather than overwriting the old ones — an unbounded, silent
  duplication for any actively-growing small log, which is a common shape
  right after a fresh rotation. Below the 4 KiB threshold the signature is
  now device and inode alone, which an in-place append cannot change.
  `docs/INGEST.md`'s troubleshooting section previously described the
  symptom as expected behaviour rather than the defect it was; corrected.

## [1.0.0] - 2026-08-17

Initial release.

### Added
- Project scaffold: packaging, configuration, error hierarchy, logging setup and CLI entry point.
- Pluggable storage abstraction (`StorageBackend`) behind which the entire inverted
  index lives: indices, documents, segments, postings, field-length norms, deletions
  and ingestion checkpoints.
- A backend-agnostic postings codec (`scopiengine.storage.codec`) — self-describing,
  varint-delta-encoded, lazily decoded — so a term matching millions of documents
  never materialises a list.
- SQLite storage backend (`sqlite://`), the zero-configuration default, with WAL
  journalling and an ordered `terms` primary key that serves prefix and range term
  scans without a secondary index.
- MS SQL Server storage backend (`mssql://`, the `scopiengine[mssql]` extra), with
  `MERGE`-based upserts, page compression on the largest tables, and
  `READ_COMMITTED_SNAPSHOT` enabled at migration time so search never blocks ingest.
- `open_storage()` DSN factory with pluggable scheme registration; `mongodb://` is
  registered but reports unavailable until 1.1.
- `scopi storage init|info|migrate` CLI commands.
- `docs/STORAGE_BACKENDS.md` and an MS SQL Server CI job.
- Text analysis (`scopiengine.analysis`): `standard`/`whitespace`/`keyword`/`pattern`/`log`
  tokenizers; `lowercase`/`stop`/`length`/`asciifolding`/`truncate` filters; an
  `Analyzer` (tokenizer + filter chain) that preserves a removed token's original
  position as a gap, so a phrase query is never silently wrong across a dropped
  stop word; an `AnalyzerRegistry` seeded with `standard`, `simple`, `whitespace`,
  `keyword`, `stop_en` and `log`, extensible by plugins. No stemming — that ships
  as the reference plugin in 1.1.
- Field mapping (`scopiengine.mapping`): `text`/`keyword`/`long`/`double`/`boolean`/`date`/`object`
  field types; order-preserving term encoding (`scopiengine.mapping.encoding`) so
  numerics and dates share the same `terms` B-tree as text, property-tested against
  a large seeded random sample (negatives, zero, infinities, extremes included);
  mapping validation with per-field `analyzer`, `search_analyzer`, `index`, `store`
  and `index_options`; dynamic mapping (on by default) that adds a `text` field plus
  a `.keyword` sub-field (`ignore_above: 256`) for a new string field.
- The inverted index (`scopiengine.index`): `IndexWriter` (in-memory buffering,
  segment flush on `buffer_mb`), `force_merge`/auto-merge on `max_segments`
  (never renumbers ordinals, purges tombstoned documents from postings and norms),
  BM25 scoring (`k1=1.2`, `b=0.75`) recomputed from live postings/norms so it stays
  identical before and after a merge, and a streaming `Searcher` (heap-merge for OR,
  leapfrog join for AND, position intersection for phrase, ordered term scan for
  prefix/range) with a bounded top-N heap so memory never scales with corpus size.
  `IndexManager` owns index lifecycle, ingestion (with dynamic mapping merge),
  `refresh`, `force_merge`, `search` and `stats`.
- A minimal query AST (`scopiengine.query.ast`): `MatchAll | Term | Phrase | Prefix
  | Range | Exists | Bool(must, should, must_not, filter)`, frozen dataclasses,
  executed by the searcher today. The ScopiQL and JSON-DSL parsers that build these
  from user input are PR 4.
- **`SearchResult`** (`scopiengine.index.searcher`): `Engine.search`/
  `IndexManager.search`/`searcher.search` now return `SearchResult(hits, total)`
  instead of a bare `list[Hit]` — `total` is the true number of matching
  documents, computed by counting every match the executor already streams
  past while filling its bounded top-N heap, never the page length and never
  capped by `size`. `searcher.iter_matches` is a new, unranked streaming
  entry point (every match, no top-N heap) for aggregations and the ScopiQL
  `stats` stage. `Engine.get_mapping` is a small new convenience for callers
  that need the parsed `Mapping`, not the raw dict `get_index` returns.
- **ScopiQL** (`scopiengine.query.scopiql`): a hand-written tokenizer and
  recursive-descent parser (no parser-generator dependency) for a compact,
  log-search-first query language — `field:value`, comparisons (`>=`/`>`/`<=`/`<`),
  inclusive ranges (`[a TO b]`), relative time (`now`/`now-1h`/`now+30m`,
  resolved against a caller-suppliable instant for deterministic tests),
  quoted phrases, `*` prefixes, `field:(a OR b)` groups, `_exists_`, bare
  terms/phrases matched across every `text` field, and `AND`/`OR`/`NOT`
  (`&&`/`||`/`!`) with implicit `AND` between adjacent terms. Pipe stages:
  `sort`, `limit`, `fields`, `stats count() by <field>`. Parse errors name the
  offending character position and what was expected there, not just
  "invalid syntax." A field the index has no mapping for parses fine and
  simply matches nothing, by design — see `docs/QUERY_LANGUAGE.md`.
- **The JSON DSL** (`scopiengine.query.dsl`): a deliberate Elasticsearch query
  DSL subset — `match`, `match_phrase`, `match_all`, `term`, `terms`,
  `prefix`, `range`, `exists`, `bool` (`must`/`should`/`must_not`/`filter`),
  plus top-level `from`, `size`, `sort`, `_source`, `aggs`/`aggregations`
  (`terms` only) — compiled through the exact same field-resolution code
  ScopiQL uses (`scopiengine.query.compiler`), so the two languages produce
  byte-identical AST for equivalent queries. Any unsupported or misspelled
  clause/option raises `UnsupportedFeatureError` naming the exact key —
  never silently ignored. Full compatibility matrix in
  `docs/QUERY_LANGUAGE.md`.
- **`scopiengine.query.results`**: the Elasticsearch-shaped response envelope
  (`took`, `hits.total.value`, `hits.hits[]` with `_id`/`_score`/`_source`,
  `aggregations`, plus a `scopi` block carrying the compiled query AST,
  segments touched and timings) and `run_scopiql`/`run_dsl`, the two entry
  points the REST API and `scopi search` both call — so a query gives the
  same answer whether or not a server happens to be running. Includes a
  `terms` aggregation (bucket-by-field, counted) shared by ScopiQL's `stats`
  stage and the DSL's `aggs`.
- **A field `sort` (`| sort <field>`, or the DSL's `sort`) is a genuine
  index-wide sort**, not a re-sort of a relevance-ranked page: it streams
  every match, fetches each candidate's sort key from stored source in
  batches, and keeps a bounded record of the true top `from_ + size` by that
  field — so "the 20 most recent errors" means the actual 20 most recent,
  not 20 arbitrary high-relevance matches reordered among themselves. Bounded
  by the new `Settings.max_sort_candidates` (default `10000`,
  `SCOPI_MAX_SORT_CANDIDATES`); a sort that has to stop before examining
  every match sets `scopi.sort_truncated: true` (with `scopi.max_sort_candidates`)
  rather than silently returning an incomplete result indistinguishable from
  a complete one, logs a warning naming the setting, and still reports the
  true, uncapped `hits.total.value`. A pure `_score` sort (the default) needs
  none of this and keeps the original single-pass path.
- **The REST API** (`scopiengine.api`, FastAPI): `GET /`, `GET /_health`,
  `GET /_cluster/health`, `GET /_cat/indices`, `PUT|GET|HEAD|DELETE /{index}`,
  `POST /{index}/_refresh`, `POST /{index}/_forcemerge`, `GET /{index}/_stats`,
  `POST /{index}/_doc`, `PUT|GET|DELETE /{index}/_doc/{id}`,
  `POST /_bulk` and `POST /{index}/_bulk` (ES NDJSON, `index`/`create`/`delete`,
  per-item results and an `errors` flag), `GET|POST /{index}/_search`
  (`?q=` for ScopiQL, a JSON body for the DSL), `POST /{index}/_scopiql`,
  `POST /{index}/_analyze`, `GET /_plugins`. One `Engine` opens at ASGI
  startup (a `lifespan` handler) and is shared by every request; search and
  indexing endpoints are synchronous `def`s on purpose, so Starlette runs
  them in its worker threadpool and the synchronous storage layer never
  blocks the event loop. Every `ScopiError` renders through one exception
  handler as the same ES-shaped envelope, at the matching status.
- CLI additions: `scopi index create|list|show|delete|stats|refresh|merge`,
  `scopi doc put|get|delete`, `scopi bulk <index> --file docs.ndjson`,
  `scopi search <index> "<scopiql>" [--dsl f.json] [--size] [--from] [--json] [--explain]`,
  `scopi analyze --analyzer <name> "<text>"`, `scopi plugin list`,
  `scopi serve [--host] [--port]`. The CLI drives the engine in process, no
  server required; `doc put`/`bulk` refresh automatically before exiting
  (each CLI invocation is its own short-lived process, unlike `scopi serve`,
  so a write left unflushed would otherwise be durably stored but silently
  unsearchable forever).
- `Dockerfile` (`python:3.11-slim`, non-root user, `EXPOSE 9500`, no system
  packages needed for the default `sqlite://` backend) and `docker-compose.yml`
  (a named volume for `/data`, a commented-out MS SQL Server profile).
- `docs/QUERY_LANGUAGE.md`: the full ScopiQL grammar, worked examples, and an
  explicit ES DSL compatibility matrix (supported/unsupported, with what to
  use instead). `docs/USAGE.md` now documents every command group with real
  syntax, and the REST API's full endpoint table.
- A ScopiQL/DSL parity test table (`tests/integration/test_api.py`):
  equivalent `(ScopiQL, DSL)` query pairs are asserted to return identical
  hit ids **and** identical scores, in order — the guarantee that the
  shorter syntax is never a second-class citizen.
- A plugin system (`scopiengine.plugins`) with four hooks — `analyzer`,
  `ingest_processor` (contract only; PR 5 supplies real processors),
  `storage_backend`, `event` — discovered from built-ins, `importlib.metadata`
  entry points (`scopiengine.plugins` group) and `SCOPI_PLUGINS=mod1,mod2`. A
  raising plugin is logged and skipped; `strict_plugins` turns that into a
  startup `PluginError`. Built-ins register through the identical hook path,
  with no privileged route.
- `Engine` (`scopiengine.engine`), the single facade for the REST API and the CLI:
  create/delete/list index, `index_documents`/`bulk`, `get_document`/`delete_document`,
  `refresh`/`force_merge`, `search`, `stats`, `analyze`.
- `examples/plugins/scopi_plugin_sample`, a real, `pip install -e`-able plugin
  package (a `shout` analyzer and a `dummy://` in-memory storage backend) used
  by `tests/integration/test_plugin_discovery.py` to exercise entry-point
  discovery against a genuinely installed package.
- `docs/ARCHITECTURE.md` and `docs/PLUGINS.md`.
- **Resilient log ingestion** (`scopiengine.ingest`): a streaming pipeline —
  `ChunkedByteReader` (fixed-size chunked reads, never materialises the
  source; a multi-byte character split across a chunk boundary always
  decodes correctly; an over-long line is truncated, not unbounded) ->
  `RecordAssembler` (one line = one record by default, `--multiline-start
  REGEX` folds continuation lines; a still-open multiline group is never
  handed back until a new record starts or the source is confirmed
  exhausted, so a crash mid-stack-trace resumes at a record boundary, never
  mid-record) -> a bounded `queue.Queue(maxsize=queue_size)` (the entire
  backpressure mechanism — peak memory has a hard ceiling independent of
  file size) -> the processor chain -> `Batcher` (flush on `batch_size`,
  `batch_bytes` or `flush_interval`, whichever fires first).
- **The single-transaction guarantee**: a batch's checkpoint update is issued
  inside the same backend transaction as its segment and document writes,
  made structural (not merely intended) by every storage backend's
  `transaction()` being reentrant on one connection — `IngestPipeline._flush`
  wraps `engine.index_documents` + `engine.flush` + `storage.save_checkpoint`
  in one `with storage.transaction():` block, so a crash at any point leaves
  the backend in exactly one of two states, never a third. Verified with a
  genuine `SIGKILL` mid-run in a real subprocess
  (`tests/integration/test_ingest_crash_resume.py`): resumed document count
  and id set are identical to an uninterrupted run.
- `scopiengine.ingest.checkpoint`: `cp_key = blake2b(index|abspath)[:16]`,
  `source_sig = f"{st_dev}:{st_ino}:{sha1(first 4 KiB)}"`, and the pure
  resume/rotation decision matrix (`decide_resume`, `classify_change`) that
  tells "resume", "truncated in place" (copytruncate) and "rotated or
  replaced" apart — the last two handled distinctly, including draining an
  already-open, rotated-out file handle to EOF before switching to the new
  file so its tail is never lost.
- `Engine.flush`/`IndexManager.flush`: a segment flush without the
  `refresh`/auto-merge check `refresh` also does — split out so ingestion's
  per-batch flush stays O(batches) instead of O(batches²) (a merge rewrites
  every live segment each time it fires); segments simply accumulate during
  a run, and `scopi index merge`/`refresh` afterward folds them back down.
- Three built-in `ingest_processor`s (`scopiengine.plugins.builtin.processors`,
  registered through the same hook a third-party plugin uses): `json_line`
  (non-JSON lines fall back to a raw `message` field rather than being
  dropped), `regex_extract` (named groups become fields, via
  `--regex-pattern`/the REST body's `regex_pattern`), `timestamp` (parses
  epoch seconds/milliseconds, ISO-8601, RFC 2822, Apache/nginx combined log
  format and classic syslog into an ISO-8601 `@timestamp`, narrowest-match-first
  so the common single-token cases resolve on the first, cheapest attempt).
- `scopi ingest file|status|reset` and `POST /_ingest/file`,
  `GET /_ingest/jobs[/{id}]`, `DELETE /_ingest/jobs/{id}` (background jobs,
  `scopiengine.ingest.jobs.IngestJobManager`). `--follow` tails a growing
  file; `Ctrl-C`/`SIGTERM` (CLI) or the `DELETE` endpoint (REST) trigger a
  clean stop — drain the queue, flush the current batch and checkpoint, exit
  `0` — so a stop is always cleanly resumable, never a lossy abort.
- `docs/INGEST.md`: the pipeline, the single-transaction guarantee,
  checkpoint/resume/rotation semantics, `--id-mode`, tuning, honest
  throughput numbers and a troubleshooting section.
- `examples/mappings/logs.json` and `examples/logs/sample-app.log`: a
  realistic explicit log mapping and a matching sample JSON-lines log file,
  used by the README quickstart and `docs/INGEST.md`.
- A feature matrix and an ES DSL compatibility summary in `README.md`.

[Unreleased]: https://github.com/AlperenCK/ScopiEngine/compare/1.0.0...HEAD
[1.0.0]: https://github.com/AlperenCK/ScopiEngine/releases/tag/1.0.0
