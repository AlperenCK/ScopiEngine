# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

## [1.0.0] - unreleased

Initial release. See the [Unreleased](#unreleased) section while 1.0.0 is being assembled.

[Unreleased]: https://github.com/AlperenCK/ScopiEngine/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/AlperenCK/ScopiEngine/releases/tag/v1.0.0
