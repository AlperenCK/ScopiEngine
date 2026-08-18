"""MS SQL Server schema for the storage backend.

Same shape as :mod:`scopiengine.storage.sqlite_ddl`, translated to T-SQL. The
deltas, and why each one exists:

- ``INT IDENTITY(1,1)`` replaces SQLite's ``AUTOINCREMENT``.
- ``PRIMARY KEY CLUSTERED`` replaces ``WITHOUT ROWID`` — both make the primary
  key the table's physical row order, which is what lets ``terms`` serve ordered
  term scans without a secondary index.
- ``term`` is ``NVARCHAR(450)``: SQL Server caps a clustered/nonclustered index
  key at 900 bytes, and ``NVARCHAR`` is 2 bytes per character, so 450 is the most
  room a term can have while still fitting.
- ``postings``/``source``/``norms`` are ``VARBINARY(MAX)``; JSON columns are
  ``NVARCHAR(MAX)``; timestamps are ISO-8601 ``NVARCHAR(32)``, so they round-trip verbatim.
- ``terms`` and ``documents`` carry ``WITH (DATA_COMPRESSION = PAGE)`` — both are
  dominated by large, repetitive blob and text payloads that compress well.
"""

from __future__ import annotations

__all__ = ["MIGRATION_STATEMENTS", "SCHEMA", "SCHEMA_VERSION"]

#: Bumped whenever the schema shape changes; stored in ``scopi.meta``.
SCHEMA_VERSION = 1

#: Every object lives under this schema so ScopiEngine can share a database.
SCHEMA = "scopi"

#: Executed once, in order, the first time a database is opened. Each statement
#: is idempotent (guarded by an existence check) so ``migrate()`` is safe to
#: call again against an already-migrated database.
MIGRATION_STATEMENTS: tuple[str, ...] = (
    "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'scopi') EXEC('CREATE SCHEMA scopi')",
    """
    IF OBJECT_ID('scopi.meta', 'U') IS NULL
    CREATE TABLE scopi.meta (
        [key]   NVARCHAR(200)  NOT NULL PRIMARY KEY CLUSTERED,
        [value] NVARCHAR(MAX)  NOT NULL
    )
    """,
    """
    IF OBJECT_ID('scopi.indices', 'U') IS NULL
    CREATE TABLE scopi.indices (
        index_id      INT IDENTITY(1,1) NOT NULL PRIMARY KEY CLUSTERED,
        name          NVARCHAR(450)  NOT NULL,
        mapping_json  NVARCHAR(MAX)  NOT NULL,
        settings_json NVARCHAR(MAX)  NOT NULL,
        doc_count     INT NOT NULL DEFAULT 0,
        next_ord      INT NOT NULL DEFAULT 0,
        next_segment  INT NOT NULL DEFAULT 0,
        created_at    NVARCHAR(32)  NOT NULL,
        CONSTRAINT ux_indices_name UNIQUE (name)
    )
    """,
    """
    IF OBJECT_ID('scopi.fields', 'U') IS NULL
    CREATE TABLE scopi.fields (
        index_id INT NOT NULL,
        field_id INT NOT NULL,
        name     NVARCHAR(450) NOT NULL,
        CONSTRAINT pk_fields PRIMARY KEY CLUSTERED (index_id, field_id),
        CONSTRAINT ux_fields_name UNIQUE (index_id, name)
    )
    """,
    """
    IF OBJECT_ID('scopi.documents', 'U') IS NULL
    CREATE TABLE scopi.documents (
        index_id INT NOT NULL,
        doc_ord  INT NOT NULL,
        doc_id   NVARCHAR(450) NOT NULL,
        source   VARBINARY(MAX) NOT NULL,
        CONSTRAINT pk_documents PRIMARY KEY CLUSTERED (index_id, doc_ord),
        CONSTRAINT ux_documents_id UNIQUE (index_id, doc_id)
    ) WITH (DATA_COMPRESSION = PAGE)
    """,
    """
    IF OBJECT_ID('scopi.segments', 'U') IS NULL
    CREATE TABLE scopi.segments (
        index_id   INT NOT NULL,
        segment_id INT NOT NULL,
        base_ord   INT NOT NULL,
        doc_count  INT NOT NULL,
        state      INT NOT NULL DEFAULT 1,
        created_at NVARCHAR(32) NOT NULL,
        CONSTRAINT pk_segments PRIMARY KEY CLUSTERED (index_id, segment_id)
    )
    """,
    """
    IF OBJECT_ID('scopi.terms', 'U') IS NULL
    CREATE TABLE scopi.terms (
        index_id   INT NOT NULL,
        field_id   INT NOT NULL,
        term       NVARCHAR(450) NOT NULL,
        segment_id INT NOT NULL,
        doc_freq   INT NOT NULL,
        total_tf   INT NOT NULL,
        postings   VARBINARY(MAX) NOT NULL,
        CONSTRAINT pk_terms PRIMARY KEY CLUSTERED (index_id, field_id, term, segment_id)
    ) WITH (DATA_COMPRESSION = PAGE)
    """,
    """
    IF OBJECT_ID('scopi.field_norms', 'U') IS NULL
    CREATE TABLE scopi.field_norms (
        index_id     INT NOT NULL,
        field_id     INT NOT NULL,
        segment_id   INT NOT NULL,
        base_ord     INT NOT NULL,
        doc_count    INT NOT NULL,
        total_length INT NOT NULL,
        norms        VARBINARY(MAX) NOT NULL,
        CONSTRAINT pk_field_norms PRIMARY KEY CLUSTERED (index_id, field_id, segment_id)
    )
    """,
    """
    IF OBJECT_ID('scopi.deletions', 'U') IS NULL
    CREATE TABLE scopi.deletions (
        index_id INT NOT NULL,
        doc_ord  INT NOT NULL,
        CONSTRAINT pk_deletions PRIMARY KEY CLUSTERED (index_id, doc_ord)
    )
    """,
    """
    IF OBJECT_ID('scopi.ingest_checkpoints', 'U') IS NULL
    CREATE TABLE scopi.ingest_checkpoints (
        cp_key        NVARCHAR(450) NOT NULL PRIMARY KEY CLUSTERED,
        index_name    NVARCHAR(450) NOT NULL,
        source_uri    NVARCHAR(MAX) NOT NULL,
        source_sig    NVARCHAR(450) NOT NULL,
        byte_offset   BIGINT NOT NULL DEFAULT 0,
        record_count  BIGINT NOT NULL DEFAULT 0,
        state         NVARCHAR(50) NOT NULL,
        error         NVARCHAR(MAX) NULL,
        started_at    NVARCHAR(32)  NOT NULL,
        updated_at    NVARCHAR(32)  NOT NULL
    )
    """,
    """
    IF OBJECT_ID('scopi.ui_accounts', 'U') IS NULL
    CREATE TABLE scopi.ui_accounts (
        username      NVARCHAR(256) NOT NULL PRIMARY KEY CLUSTERED,
        password_hash NVARCHAR(450) NOT NULL,
        disabled      BIT NOT NULL DEFAULT 0,
        created_at    NVARCHAR(32)  NOT NULL
    )
    """,
    """
    IF OBJECT_ID('scopi.ui_sessions', 'U') IS NULL
    CREATE TABLE scopi.ui_sessions (
        session_id_hash NVARCHAR(64)  NOT NULL PRIMARY KEY CLUSTERED,
        principal        NVARCHAR(256) NOT NULL,
        auth_method      NVARCHAR(50)  NOT NULL,
        created_at       NVARCHAR(32)  NOT NULL,
        expires_at       NVARCHAR(32)  NOT NULL
    )
    """,
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes WHERE name = 'ix_ui_sessions_expires_at'
    )
    CREATE INDEX ix_ui_sessions_expires_at ON scopi.ui_sessions (expires_at)
    """,
)
