"""SQLite schema for the storage backend.

``WITHOUT ROWID`` tables are used wherever the primary key is already the natural
clustering key — it saves the hidden rowid and its extra b-tree lookup. The
``terms`` primary key is ordered ``(index_id, field_id, term, segment_id)``, which
is what lets :meth:`~scopiengine.storage.sqlite_backend.SQLiteBackend.iter_terms`
serve prefix and range queries with a plain ``WHERE term >= ? AND term < ?`` scan
instead of a secondary index.
"""

from __future__ import annotations

__all__ = ["MIGRATION_STATEMENTS", "PRAGMA_STATEMENTS", "SCHEMA_VERSION"]

#: Bumped whenever the schema shape changes; stored in ``scopi_meta``.
SCHEMA_VERSION = 1

#: Applied on every connection open, before any query runs.
PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA temp_store = MEMORY",
)

#: Executed once, in order, the first time a database is opened.
MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS scopi_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS indices (
        index_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL UNIQUE,
        mapping_json  TEXT NOT NULL,
        settings_json TEXT NOT NULL,
        doc_count     INTEGER NOT NULL DEFAULT 0,
        next_ord      INTEGER NOT NULL DEFAULT 0,
        next_segment  INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fields (
        index_id INTEGER NOT NULL,
        field_id INTEGER NOT NULL,
        name     TEXT NOT NULL,
        PRIMARY KEY (index_id, field_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_fields_name
        ON fields (index_id, name)
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        index_id INTEGER NOT NULL,
        doc_ord  INTEGER NOT NULL,
        doc_id   TEXT NOT NULL,
        source   BLOB NOT NULL,
        PRIMARY KEY (index_id, doc_ord)
    ) WITHOUT ROWID
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_id
        ON documents (index_id, doc_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS segments (
        index_id   INTEGER NOT NULL,
        segment_id INTEGER NOT NULL,
        base_ord   INTEGER NOT NULL,
        doc_count  INTEGER NOT NULL,
        state      INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY (index_id, segment_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS terms (
        index_id   INTEGER NOT NULL,
        field_id   INTEGER NOT NULL,
        term       TEXT NOT NULL,
        segment_id INTEGER NOT NULL,
        doc_freq   INTEGER NOT NULL,
        total_tf   INTEGER NOT NULL,
        postings   BLOB NOT NULL,
        PRIMARY KEY (index_id, field_id, term, segment_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS field_norms (
        index_id     INTEGER NOT NULL,
        field_id     INTEGER NOT NULL,
        segment_id   INTEGER NOT NULL,
        base_ord     INTEGER NOT NULL,
        doc_count    INTEGER NOT NULL,
        total_length INTEGER NOT NULL,
        norms        BLOB NOT NULL,
        PRIMARY KEY (index_id, field_id, segment_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS deletions (
        index_id INTEGER NOT NULL,
        doc_ord  INTEGER NOT NULL,
        PRIMARY KEY (index_id, doc_ord)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_checkpoints (
        cp_key        TEXT PRIMARY KEY,
        index_name    TEXT NOT NULL,
        source_uri    TEXT NOT NULL,
        source_sig    TEXT NOT NULL,
        byte_offset   INTEGER NOT NULL DEFAULT 0,
        record_count  INTEGER NOT NULL DEFAULT 0,
        state         TEXT NOT NULL,
        error         TEXT,
        started_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
)
