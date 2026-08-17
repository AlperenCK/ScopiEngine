# Storage backends

ScopiEngine's inverted index — indices, documents, segments, postings, norms and
ingestion checkpoints — lives entirely behind one interface:
[`StorageBackend`](../src/scopiengine/storage/base.py). Nothing above that
interface knows or cares whether the bytes end up in SQLite, MS SQL Server, or
(from 1.1) MongoDB. Swapping backends is a DSN change, not a code change.

## Choosing a backend by DSN

`scopi` and the engine both take a single storage DSN — on the command line with
`-s`/`--storage`, as `SCOPI_STORAGE`, or in a config file's `storage` key. The
scheme selects the backend:

| Scheme | Example DSN | Status |
|---|---|---|
| `sqlite` | `sqlite:///./data/scopi.db` | Built in, zero configuration |
| `sqlite` (in-memory) | `sqlite:///:memory:` | Built in, non-durable, mainly for tests |
| `mssql` | `mssql://user:pw@host:1433/scopi?driver=ODBC+Driver+18+for+SQL+Server` | Built in, requires the `mssql` extra |
| `mongodb` | `mongodb://host/scopi` | Registered, raises `BackendNotAvailableError` until 1.1 |

```bash
scopi --storage "sqlite:///./data/scopi.db" storage init
scopi --storage "mssql://scopi:s3cret@sqlhost:1433/scopi?driver=ODBC+Driver+18+for+SQL+Server" storage init
```

`scopi storage` has three subcommands, all respecting the global `--json` flag:

| Command | Purpose |
|---|---|
| `scopi storage init` | Create the schema on a fresh database (idempotent). |
| `scopi storage migrate` | Apply pending migrations; same as `init` once the schema already exists. |
| `scopi storage info` | Print the resolved DSN, scheme, schema version, and whether the backend answers. |

## Why postings are blob segments, not a row per posting

A term's postings list — which documents contain it, how often, at which
positions — could be stored two ways:

1. **One row per posting.** Simple, but the row count explodes: a single field
   of typical log text produces tens of postings per document. A modest 1 GB of
   logs is roughly 100 million postings, so this is 100 million rows, each with
   B-tree overhead, and lands around 6–10 GB on disk once indexes are counted.
2. **One blob per (term, segment).** Every document containing a term is
   delta-encoded into a single binary blob (see [Postings codec](#postings-codec)
   below), and that blob is the value in one row. Row count drops to
   roughly one row per distinct term per segment — thousands, not millions —
   and the encoding itself runs 1–3 bytes per posting thanks to varint delta
   encoding, so the same 100 million postings cost on the order of 100–300 MB.

ScopiEngine uses the second approach everywhere. The trade-off is that reading
a term's postings decodes a blob instead of running a row scan — which is
exactly the shape `codec.decode_postings` is built for: a generator that
decodes lazily, one posting at a time, so a term matching millions of documents
never forces the whole list into memory at once.

## Postings codec

Encoding is backend-agnostic — every backend stores the identical bytes
verbatim in a `BLOB`/`VARBINARY` column, and `scopiengine.storage.codec` is the
only place that reads or writes this layout.

```
[magic u8][flags u8][doc_count varint]
per document:
    docid_gap varint
    tf varint
    if positions flag set: tf position-gap varints
```

- `magic` identifies the format so a misrouted blob fails fast instead of
  decoding into garbage.
- `flags` bit 0 says whether per-occurrence token positions were encoded.
- Every varint is unsigned LEB128. Document ordinals and, when present, token
  positions are delta-encoded (gap from the previous value, starting at zero),
  which is what keeps the common case — nearby ordinals — small.
- `encode_postings` requires strictly ascending `doc_ord` (and ascending
  `positions` within a document, when positions are encoded); it raises
  `StorageError` otherwise, so a caller finds out immediately instead of
  writing a blob that decodes wrong.
- `decode_postings` is a generator: nothing beyond the header is touched until
  the caller asks for the next posting.

Field-length norms (used for BM25) are a separate, simpler format: one byte per
document ordinal, clamped to 255 — `encode_norms` / `decode_norms`.

## SQLite backend

The default, zero-configuration backend. No server, no extra dependency — the
standard library's `sqlite3` module is enough. Good for a laptop, a single
node, or CI.

```sql
PRAGMA journal_mode = WAL;  PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;   PRAGMA temp_store = MEMORY;

CREATE TABLE scopi_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE indices (
    index_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    mapping_json  TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    next_ord      INTEGER NOT NULL DEFAULT 0,
    next_segment  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE fields (
    index_id INTEGER NOT NULL,
    field_id INTEGER NOT NULL,
    name     TEXT NOT NULL,
    PRIMARY KEY (index_id, field_id)
) WITHOUT ROWID;
CREATE UNIQUE INDEX ux_fields_name ON fields (index_id, name);

CREATE TABLE documents (
    index_id INTEGER NOT NULL,
    doc_ord  INTEGER NOT NULL,
    doc_id   TEXT NOT NULL,
    source   BLOB NOT NULL,
    PRIMARY KEY (index_id, doc_ord)
) WITHOUT ROWID;
CREATE UNIQUE INDEX ux_documents_id ON documents (index_id, doc_id);

CREATE TABLE segments (
    index_id   INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    base_ord   INTEGER NOT NULL,
    doc_count  INTEGER NOT NULL,
    state      INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (index_id, segment_id)
) WITHOUT ROWID;

CREATE TABLE terms (
    index_id   INTEGER NOT NULL,
    field_id   INTEGER NOT NULL,
    term       TEXT NOT NULL,
    segment_id INTEGER NOT NULL,
    doc_freq   INTEGER NOT NULL,
    total_tf   INTEGER NOT NULL,
    postings   BLOB NOT NULL,
    PRIMARY KEY (index_id, field_id, term, segment_id)
) WITHOUT ROWID;

CREATE TABLE field_norms (
    index_id     INTEGER NOT NULL,
    field_id     INTEGER NOT NULL,
    segment_id   INTEGER NOT NULL,
    base_ord     INTEGER NOT NULL,
    doc_count    INTEGER NOT NULL,
    total_length INTEGER NOT NULL,
    norms        BLOB NOT NULL,
    PRIMARY KEY (index_id, field_id, segment_id)
) WITHOUT ROWID;

CREATE TABLE deletions (
    index_id INTEGER NOT NULL,
    doc_ord  INTEGER NOT NULL,
    PRIMARY KEY (index_id, doc_ord)
) WITHOUT ROWID;

CREATE TABLE ingest_checkpoints (
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
);
```

`WITHOUT ROWID` is used everywhere the primary key is already the natural
clustering key: it skips the hidden rowid and its extra b-tree lookup. The
`terms` primary key order — `(index_id, field_id, term, segment_id)` — is
deliberate: it is what lets `iter_terms` serve both prefix queries
(`WHERE term >= 'abc' AND term < 'abd'`) and range queries
(`WHERE term >= lo AND term < hi`) as a single ordered range scan over the
primary key, with no secondary index at all.

`scopi_meta` carries `schema_version` and `engine_version`. `migrate()` creates
the schema when it is absent, and refuses to open a database whose stored
`schema_version` is newer than the running build understands — raising
`StorageError` rather than silently misreading rows the newer format wrote
differently.

Every mutating call runs inside a transaction (`BEGIN IMMEDIATE` /
`COMMIT`/`ROLLBACK`); bulk writes use `executemany` for one round trip per
batch rather than one per row, and `write_segment` pulls from the postings
iterator in fixed-size chunks so a segment with millions of terms is never
held in memory as a single Python list.

## MS SQL Server backend

Same schema shape, adapted to T-SQL, under the `scopi` schema so ScopiEngine
can share a database with other applications.

| SQLite | MS SQL Server | Why |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `INT IDENTITY(1,1)` | Surrogate key generation. |
| `... WITHOUT ROWID` | `... PRIMARY KEY CLUSTERED` | Both make the primary key the table's physical row order — required for `terms`' ordered scan. |
| `term TEXT` | `term NVARCHAR(450)` | SQL Server caps an index key at 900 bytes; `NVARCHAR` is 2 bytes/char, so 450 chars is the most room a term can have while still being indexable. |
| `postings BLOB` | `postings VARBINARY(MAX)` | Unbounded binary payload. |
| `mapping_json TEXT` | `mapping_json NVARCHAR(MAX)` | Unbounded JSON text. |
| `created_at TEXT` (ISO-8601) | `created_at DATETIME2(3)` | Native timestamp, millisecond precision. |
| `INSERT ... ON CONFLICT DO UPDATE` | `MERGE` | Upsert semantics — used for checkpoints and document upserts; segment writes are pure `INSERT`, since segments are immutable once written. |
| — | `WITH (DATA_COMPRESSION = PAGE)` on `terms` and `documents` | Both tables are dominated by large, repetitive blob/text payloads that compress well. |

`migrate()` also sets `READ_COMMITTED_SNAPSHOT ON` for the database (once, if
not already set), so readers use row versioning instead of taking locks —
search never blocks behind an in-progress ingest.

### Setup

1. Install the extra: `pip install "scopiengine[mssql]"` (this pulls in
   `pyodbc`).
2. Install a Microsoft ODBC driver for SQL Server on the host. On Debian/Ubuntu
   that is the `msodbcsql18` package from Microsoft's `packages-microsoft-prod`
   repository; on macOS, `brew install msodbcsql18` (via the `microsoft/mssql-release`
   tap); on Windows it usually ships with SQL Server tooling already.
3. Point `--storage` at the server:

   ```bash
   scopi --storage "mssql://scopi:s3cret@sqlhost:1433/scopi?driver=ODBC+Driver+18+for+SQL+Server" storage init
   ```

   The `driver` query parameter names the installed ODBC driver exactly as
   `odbcinst -q -d` lists it; it defaults to `ODBC Driver 18 for SQL Server`
   when omitted. Extra query parameters (e.g. `TrustServerCertificate=yes` for
   a self-signed development certificate) are passed straight through to the
   ODBC connection string.

`pyodbc` is imported lazily, inside `MSSQLBackend.open()` — never at module
import time — so a build without the `mssql` extra installed can still import
`scopiengine.storage` freely; only actually opening an `mssql://` DSN raises
`BackendNotAvailableError`, naming the extra and the driver package.

## Writing a new backend

1. Implement `scopiengine.storage.base.StorageBackend` — every method is
   abstract, so a partial implementation fails at instantiation, not at first
   use.
2. Store `codec.encode_postings`/`encode_norms` output as opaque bytes; never
   reinterpret or re-encode it — that is what keeps a term's postings
   byte-identical across backends.
3. Make every write batch-oriented: accept iterables, use your driver's bulk
   API, and never do a per-term or per-document round trip.
4. Import optional driver dependencies lazily, inside methods, so backends
   nobody has configured never affect import time or `pip install` for
   everyone else. Raise `BackendNotAvailableError` (naming the fix) when the
   dependency is missing.
5. Register a DSN scheme with `scopiengine.storage.factory.register_backend`,
   either by adding it to `factory.py` directly, or from a plugin at import
   time.
6. Run `tests/contract/test_storage_conformance.py` against the new backend —
   add a fixture branch the same way the `mssql` one works, gated behind an
   environment variable and a custom pytest marker if the backend needs a live
   external server.
