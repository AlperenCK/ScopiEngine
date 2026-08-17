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

## [1.0.0] - unreleased

Initial release. See the [Unreleased](#unreleased) section while 1.0.0 is being assembled.

[Unreleased]: https://github.com/AlperenCK/ScopiEngine/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/AlperenCK/ScopiEngine/releases/tag/v1.0.0
