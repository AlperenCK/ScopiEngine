"""MS SQL Server implementation of :class:`~scopiengine.storage.base.StorageBackend`.

``pyodbc`` is an optional dependency (the ``scopiengine[mssql]`` extra) and is
imported lazily, inside :meth:`MSSQLBackend.open`, so this module — and therefore
:mod:`scopiengine.storage.factory` — imports cleanly on a build that never
installed it. Reaching for the backend without the extra installed raises
:class:`~scopiengine.errors.BackendNotAvailableError` with the fix.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from scopiengine import __version__
from scopiengine.errors import (
    BackendNotAvailableError,
    IndexAlreadyExistsError,
    IndexNotFoundError,
    StorageError,
)
from scopiengine.logging_conf import get_logger
from scopiengine.storage.base import SegmentTermEntry, StorageBackend
from scopiengine.storage.models import (
    Checkpoint,
    IndexInfo,
    NormsBlock,
    SegmentInfo,
    SegmentState,
    StoredDoc,
    TermStats,
)
from scopiengine.storage.mssql_ddl import MIGRATION_STATEMENTS, SCHEMA, SCHEMA_VERSION

__all__ = ["MSSQLBackend", "MSSQLConfig"]

_log = get_logger(__name__)

#: Rows per ``executemany`` batch when writing a segment's postings.
_WRITE_CHUNK_SIZE = 1000

#: Message shown when ``pyodbc`` (or its driver) is not available.
_NOT_AVAILABLE = (
    "the mssql storage backend requires the 'pyodbc' package and a Microsoft ODBC "
    'driver; install with `pip install "scopiengine[mssql]"` and the '
    "'ODBC Driver 18 for SQL Server' package for your OS, then retry"
)


class MSSQLConfig:
    """Connection parameters parsed from an ``mssql://`` DSN.

    Attributes:
        host: Server hostname.
        port: TCP port.
        database: Database name.
        user: Login name, or ``None`` to use integrated/trusted authentication.
        password: Login password.
        driver: ODBC driver name, e.g. ``ODBC Driver 18 for SQL Server``.
        options: Extra ODBC connection-string attributes from DSN query parameters.
    """

    __slots__ = ("database", "driver", "host", "options", "password", "port", "user")

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str | None,
        password: str | None,
        driver: str,
        options: dict[str, str],
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.driver = driver
        self.options = options

    def to_odbc_string(self) -> str:
        """Render an ODBC connection string for ``pyodbc.connect``."""
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.host},{self.port}",
            f"DATABASE={self.database}",
        ]
        if self.user is not None:
            parts.append(f"UID={self.user}")
            parts.append(f"PWD={self.password or ''}")
        else:
            parts.append("Trusted_Connection=yes")
        for key, value in self.options.items():
            parts.append(f"{key}={value}")
        return ";".join(parts)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string, second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_index_info(row: Any) -> IndexInfo:
    import json

    return IndexInfo(
        index_id=row.index_id,
        name=row.name,
        mapping=json.loads(row.mapping_json),
        settings=json.loads(row.settings_json),
        doc_count=row.doc_count,
        next_ord=row.next_ord,
        next_segment=row.next_segment,
        created_at=str(row.created_at),
    )


def _row_to_checkpoint(row: Any) -> Checkpoint:
    return Checkpoint(
        cp_key=row.cp_key,
        index_name=row.index_name,
        source_uri=row.source_uri,
        source_sig=row.source_sig,
        byte_offset=row.byte_offset,
        record_count=row.record_count,
        state=row.state,
        error=row.error,
        started_at=str(row.started_at),
        updated_at=str(row.updated_at),
    )


class MSSQLBackend(StorageBackend):
    """Storage backend backed by MS SQL Server, via ``pyodbc``.

    Args:
        config: Parsed connection parameters, typically built by
            :mod:`scopiengine.storage.factory` from an ``mssql://`` DSN.
    """

    def __init__(self, config: MSSQLConfig) -> None:
        self._config = config
        self._conn: Any = None
        self._tx_depth = 0

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        if self._conn is not None:
            return
        try:
            import pyodbc
        except ImportError as exc:
            raise BackendNotAvailableError(_NOT_AVAILABLE) from exc
        try:
            self._conn = pyodbc.connect(self._config.to_odbc_string(), autocommit=True)
        except pyodbc.Error as exc:
            raise StorageError(f"could not connect to MS SQL Server: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def ping(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT 1").fetchall()
        except Exception:
            return False
        return True

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise StorageError("storage backend is not open; call open() first")
        return self._conn

    def _cursor(self) -> Any:
        """A cursor with ``fast_executemany`` on, for bulk ``executemany`` calls."""
        cursor = self._require_conn().cursor()
        cursor.fast_executemany = True
        return cursor

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    def migrate(self) -> None:
        conn = self._require_conn()
        with self.transaction():
            for statement in MIGRATION_STATEMENTS:
                conn.execute(statement)
            existing = conn.execute(
                f"SELECT [value] FROM {SCHEMA}.meta WHERE [key] = 'schema_version'"
            ).fetchone()
            if existing is None:
                conn.execute(
                    f"INSERT INTO {SCHEMA}.meta ([key], [value]) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                stored = int(existing[0])
                if stored > SCHEMA_VERSION:
                    raise StorageError(
                        f"database schema version {stored} is newer than this build of "
                        f"ScopiEngine supports ({SCHEMA_VERSION}); upgrade ScopiEngine to "
                        "open this database"
                    )
            conn.execute(
                f"MERGE {SCHEMA}.meta AS target "
                "USING (SELECT 'engine_version' AS [key], ? AS [value]) AS source "
                "ON target.[key] = source.[key] "
                "WHEN MATCHED THEN UPDATE SET target.[value] = source.[value] "
                "WHEN NOT MATCHED THEN INSERT ([key], [value])"
                " VALUES (source.[key], source.[value]);",
                (__version__,),
            )
        # ALTER DATABASE is rejected inside a multi-statement transaction, so read
        # committed snapshot isolation is enabled after the schema transaction has
        # committed, never within it.
        self._enable_snapshot_isolation()

    def _enable_snapshot_isolation(self) -> None:
        """Turn on read committed snapshot isolation so search never blocks ingestion.

        This is a concurrency optimisation rather than a correctness requirement, and
        it needs a privilege a managed or shared instance may withhold. A refusal is
        therefore reported as a warning naming the statement to run by hand, instead
        of failing an otherwise successful migration.
        """
        conn = self._require_conn()
        snapshot = conn.execute(
            "SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name = DB_NAME()"
        ).fetchone()
        if snapshot is None or snapshot[0]:
            return
        statement = (
            f"ALTER DATABASE [{self._config.database}] "
            "SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE"
        )
        try:
            conn.execute(statement)
        except Exception as exc:  # the driver's error type varies by ODBC version
            _log.warning(
                "could not enable read committed snapshot isolation on %s: %s. "
                "Searches may block behind ingestion until it is enabled with: %s",
                self._config.database,
                exc,
                statement,
            )

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        conn = self._require_conn()
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return

        import pyodbc  # already imported successfully by open(), so this is cheap

        self._tx_depth = 1
        conn.autocommit = False
        try:
            yield
        except pyodbc.Error as exc:
            # A driver-level failure: translate it into the storage error hierarchy.
            conn.rollback()
            raise StorageError(f"transaction rolled back: {exc}") from exc
        except BaseException:
            # Anything else (ScopiError subclasses, caller-raised exceptions) rolls
            # back but keeps its own type and message intact.
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.autocommit = True
            self._tx_depth = 0

    # -- indices -------------------------------------------------------------

    def _get_index_row(self, name: str) -> Any:
        conn = self._require_conn()
        row = conn.execute(f"SELECT * FROM {SCHEMA}.indices WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise IndexNotFoundError(f"no such index: {name!r}")
        return row

    def create_index(
        self, name: str, mapping: dict[str, Any], settings: dict[str, Any]
    ) -> IndexInfo:
        import json

        conn = self._require_conn()
        with self.transaction():
            existing = conn.execute(
                f"SELECT 1 FROM {SCHEMA}.indices WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                raise IndexAlreadyExistsError(f"index already exists: {name!r}")
            created_at = _now_iso()
            row = conn.execute(
                f"INSERT INTO {SCHEMA}.indices"
                " (name, mapping_json, settings_json, doc_count, next_ord, next_segment,"
                "  created_at)"
                " OUTPUT INSERTED.index_id VALUES (?, ?, ?, 0, 0, 0, ?)",
                (name, json.dumps(mapping), json.dumps(settings), created_at),
            ).fetchone()
            index_id = row[0]
        return IndexInfo(
            index_id=index_id,
            name=name,
            mapping=mapping,
            settings=settings,
            doc_count=0,
            next_ord=0,
            next_segment=0,
            created_at=created_at,
        )

    def get_index(self, name: str) -> IndexInfo:
        return _row_to_index_info(self._get_index_row(name))

    def delete_index(self, name: str) -> None:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(name)
            index_id = row.index_id
            for table in ("fields", "documents", "segments", "terms", "field_norms", "deletions"):
                conn.execute(f"DELETE FROM {SCHEMA}.{table} WHERE index_id = ?", (index_id,))
            conn.execute(f"DELETE FROM {SCHEMA}.indices WHERE index_id = ?", (index_id,))

    def list_indices(self) -> list[IndexInfo]:
        conn = self._require_conn()
        rows = conn.execute(f"SELECT * FROM {SCHEMA}.indices ORDER BY name").fetchall()
        return [_row_to_index_info(row) for row in rows]

    def update_mapping(self, name: str, mapping: dict[str, Any]) -> IndexInfo:
        import json

        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(name)
            conn.execute(
                f"UPDATE {SCHEMA}.indices SET mapping_json = ? WHERE index_id = ?",
                (json.dumps(mapping), row.index_id),
            )
        return self.get_index(name)

    # -- ordinal and segment allocation ---------------------------------------

    def allocate_ords(self, index: str, n: int) -> int:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            base = row.next_ord
            conn.execute(
                f"UPDATE {SCHEMA}.indices SET next_ord = next_ord + ? WHERE index_id = ?",
                (n, row.index_id),
            )
        return int(base)

    def allocate_segment(self, index: str) -> int:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            segment_id = row.next_segment
            conn.execute(
                f"UPDATE {SCHEMA}.indices SET next_segment = next_segment + 1 WHERE index_id = ?",
                (row.index_id,),
            )
        return int(segment_id)

    def resolve_field_ids(self, index: str, names: Sequence[str]) -> dict[str, int]:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row.index_id
            result: dict[str, int] = {}
            missing: list[str] = []
            for name in names:
                found = conn.execute(
                    f"SELECT field_id FROM {SCHEMA}.fields WHERE index_id = ? AND name = ?",
                    (index_id, name),
                ).fetchone()
                if found is None:
                    if name not in missing:
                        missing.append(name)
                else:
                    result[name] = found[0]
            if missing:
                next_row = conn.execute(
                    f"SELECT COALESCE(MAX(field_id), -1) FROM {SCHEMA}.fields WHERE index_id = ?",
                    (index_id,),
                ).fetchone()
                next_id = next_row[0] + 1
                to_insert = []
                for name in missing:
                    result[name] = next_id
                    to_insert.append((index_id, next_id, name))
                    next_id += 1
                cursor = self._cursor()
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.fields (index_id, field_id, name) VALUES (?, ?, ?)",
                    to_insert,
                )
        return result

    # -- documents -------------------------------------------------------------

    def put_documents(self, index: str, docs: Iterable[StoredDoc]) -> None:
        conn = self._require_conn()
        batch = list(docs)
        if not batch:
            self._get_index_row(index)
            return
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row.index_id
            ords = [doc.doc_ord for doc in batch]
            placeholders = ",".join("?" * len(ords))
            existing = conn.execute(
                f"SELECT doc_ord FROM {SCHEMA}.documents"
                f" WHERE index_id = ? AND doc_ord IN ({placeholders})",
                [index_id, *ords],
            ).fetchall()
            new_count = len(batch) - len(existing)
            cursor = self._cursor()
            cursor.executemany(
                f"MERGE {SCHEMA}.documents AS target "
                "USING (SELECT ? AS index_id, ? AS doc_ord, ? AS doc_id, ? AS source) AS source "
                "ON target.index_id = source.index_id AND target.doc_ord = source.doc_ord "
                "WHEN MATCHED THEN UPDATE SET doc_id = source.doc_id, source = source.source "
                "WHEN NOT MATCHED THEN INSERT (index_id, doc_ord, doc_id, source) "
                "VALUES (source.index_id, source.doc_ord, source.doc_id, source.source);",
                [(index_id, doc.doc_ord, doc.doc_id, doc.source) for doc in batch],
            )
            if new_count:
                conn.execute(
                    f"UPDATE {SCHEMA}.indices SET doc_count = doc_count + ? WHERE index_id = ?",
                    (new_count, index_id),
                )

    def get_documents(self, index: str, ords: Sequence[int]) -> list[StoredDoc]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        if not ords:
            return []
        index_id = row.index_id
        placeholders = ",".join("?" * len(ords))
        rows = conn.execute(
            f"SELECT doc_ord, doc_id, source FROM {SCHEMA}.documents"
            f" WHERE index_id = ? AND doc_ord IN ({placeholders})",
            [index_id, *ords],
        ).fetchall()
        by_ord = {r.doc_ord: r for r in rows}
        return [
            StoredDoc(doc_ord=o, doc_id=by_ord[o].doc_id, source=bytes(by_ord[o].source))
            for o in ords
            if o in by_ord
        ]

    def resolve_ids(self, index: str, doc_ids: Sequence[str]) -> dict[str, int]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        if not doc_ids:
            return {}
        index_id = row.index_id
        placeholders = ",".join("?" * len(doc_ids))
        rows = conn.execute(
            f"SELECT doc_id, doc_ord FROM {SCHEMA}.documents"
            f" WHERE index_id = ? AND doc_id IN ({placeholders})",
            [index_id, *doc_ids],
        ).fetchall()
        return {r.doc_id: r.doc_ord for r in rows}

    def mark_deleted(self, index: str, ords: Sequence[int]) -> None:
        if not ords:
            self._get_index_row(index)
            return
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row.index_id
            cursor = self._cursor()
            cursor.executemany(
                f"IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.deletions"
                " WHERE index_id = ? AND doc_ord = ?)"
                f" INSERT INTO {SCHEMA}.deletions (index_id, doc_ord) VALUES (?, ?)",
                [(index_id, o, index_id, o) for o in ords],
            )

    def iter_deleted(self, index: str) -> Iterator[int]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        cur = conn.execute(
            f"SELECT doc_ord FROM {SCHEMA}.deletions WHERE index_id = ? ORDER BY doc_ord",
            (row.index_id,),
        )
        return (r.doc_ord for r in cur)

    # -- segments and postings -------------------------------------------------

    def write_segment(
        self,
        index: str,
        segment_id: int,
        base_ord: int,
        doc_count: int,
        postings_iter: Iterable[SegmentTermEntry],
        norms: Iterable[NormsBlock],
    ) -> None:
        import itertools

        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row.index_id
            conn.execute(
                f"INSERT INTO {SCHEMA}.segments"
                " (index_id, segment_id, base_ord, doc_count, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (index_id, segment_id, base_ord, doc_count, int(SegmentState.LIVE), _now_iso()),
            )
            iterator = iter(postings_iter)
            while chunk := list(itertools.islice(iterator, _WRITE_CHUNK_SIZE)):
                cursor = self._cursor()
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.terms"
                    " (index_id, field_id, term, segment_id, doc_freq, total_tf, postings)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (index_id, field_id, term, segment_id, doc_freq, total_tf, blob)
                        for field_id, term, blob, doc_freq, total_tf in chunk
                    ],
                )
            norms_rows = [
                (
                    index_id,
                    block.field_id,
                    segment_id,
                    block.base_ord,
                    block.doc_count,
                    block.total_length,
                    block.norms,
                )
                for block in norms
            ]
            if norms_rows:
                cursor = self._cursor()
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.field_norms"
                    " (index_id, field_id, segment_id, base_ord, doc_count, total_length, norms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    norms_rows,
                )

    def list_segments(self, index: str) -> list[SegmentInfo]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        rows = conn.execute(
            f"SELECT * FROM {SCHEMA}.segments WHERE index_id = ? ORDER BY base_ord",
            (row.index_id,),
        ).fetchall()
        return [
            SegmentInfo(
                segment_id=r.segment_id,
                base_ord=r.base_ord,
                doc_count=r.doc_count,
                state=SegmentState(r.state),
                created_at=str(r.created_at),
            )
            for r in rows
        ]

    def drop_segments(self, index: str, segment_ids: Sequence[int]) -> None:
        conn = self._require_conn()
        if not segment_ids:
            self._get_index_row(index)
            return
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row.index_id
            placeholders = ",".join("?" * len(segment_ids))
            for table in ("segments", "terms", "field_norms"):
                conn.execute(
                    f"DELETE FROM {SCHEMA}.{table}"
                    f" WHERE index_id = ? AND segment_id IN ({placeholders})",
                    [index_id, *segment_ids],
                )

    def get_postings(self, index: str, field_id: int, term: str) -> list[tuple[int, int, bytes]]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        rows = conn.execute(
            f"SELECT t.segment_id AS segment_id, s.base_ord AS base_ord, t.postings AS postings"
            f" FROM {SCHEMA}.terms t JOIN {SCHEMA}.segments s"
            " ON s.index_id = t.index_id AND s.segment_id = t.segment_id"
            " WHERE t.index_id = ? AND t.field_id = ? AND t.term = ? AND s.state != ?",
            (row.index_id, field_id, term, int(SegmentState.DEAD)),
        ).fetchall()
        return [(r.segment_id, r.base_ord, bytes(r.postings)) for r in rows]

    def iter_terms(
        self,
        index: str,
        field_id: int,
        *,
        prefix: str | None = None,
        lo: str | None = None,
        hi: str | None = None,
    ) -> Iterator[str]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        index_id = row.index_id

        lower: str | None
        upper: str | None
        if prefix is not None:
            lower, upper = prefix, _prefix_upper_bound(prefix)
        else:
            lower, upper = lo, hi

        query = f"SELECT DISTINCT term FROM {SCHEMA}.terms WHERE index_id = ? AND field_id = ?"
        params: list[Any] = [index_id, field_id]
        if lower is not None:
            query += " AND term >= ?"
            params.append(lower)
        if upper is not None:
            query += " AND term < ?"
            params.append(upper)
        query += " ORDER BY term"

        cur = conn.execute(query, params)
        return (r.term for r in cur)

    def get_term_stats(self, index: str, field_id: int, term: str) -> TermStats:
        conn = self._require_conn()
        row = self._get_index_row(index)
        stats = conn.execute(
            "SELECT COALESCE(SUM(doc_freq), 0) AS doc_freq,"
            " COALESCE(SUM(total_tf), 0) AS total_tf"
            f" FROM {SCHEMA}.terms t JOIN {SCHEMA}.segments s"
            " ON s.index_id = t.index_id AND s.segment_id = t.segment_id"
            " WHERE t.index_id = ? AND t.field_id = ? AND t.term = ? AND s.state != ?",
            (row.index_id, field_id, term, int(SegmentState.DEAD)),
        ).fetchone()
        return TermStats(term=term, doc_freq=stats.doc_freq, total_tf=stats.total_tf)

    def get_norms(self, index: str, field_id: int, segment_id: int) -> NormsBlock | None:
        conn = self._require_conn()
        row = self._get_index_row(index)
        found = conn.execute(
            f"SELECT base_ord, doc_count, total_length, norms FROM {SCHEMA}.field_norms"
            " WHERE index_id = ? AND field_id = ? AND segment_id = ?",
            (row.index_id, field_id, segment_id),
        ).fetchone()
        if found is None:
            return None
        return NormsBlock(
            field_id=field_id,
            segment_id=segment_id,
            base_ord=found.base_ord,
            doc_count=found.doc_count,
            total_length=found.total_length,
            norms=bytes(found.norms),
        )

    def index_stats(self, index: str) -> dict[str, Any]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        index_id = row.index_id
        deleted = conn.execute(
            f"SELECT COUNT(*) FROM {SCHEMA}.deletions WHERE index_id = ?", (index_id,)
        ).fetchone()[0]
        segment_count = conn.execute(
            f"SELECT COUNT(*) FROM {SCHEMA}.segments WHERE index_id = ?", (index_id,)
        ).fetchone()[0]
        return {
            "index": index,
            "doc_count": row.doc_count,
            "deleted_count": deleted,
            "live_doc_count": max(row.doc_count - deleted, 0),
            "segment_count": segment_count,
            "next_ord": row.next_ord,
            "next_segment": row.next_segment,
        }

    # -- ingestion checkpoints ---------------------------------------------

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        conn = self._require_conn()
        with self.transaction():
            conn.execute(
                f"MERGE {SCHEMA}.ingest_checkpoints AS target "
                "USING (SELECT ? AS cp_key, ? AS index_name, ? AS source_uri, ? AS source_sig,"
                " ? AS byte_offset, ? AS record_count, ? AS state, ? AS error,"
                " ? AS started_at, ? AS updated_at) AS source "
                "ON target.cp_key = source.cp_key "
                "WHEN MATCHED THEN UPDATE SET"
                " index_name = source.index_name, source_uri = source.source_uri,"
                " source_sig = source.source_sig, byte_offset = source.byte_offset,"
                " record_count = source.record_count, state = source.state,"
                " error = source.error, updated_at = source.updated_at "
                "WHEN NOT MATCHED THEN INSERT"
                " (cp_key, index_name, source_uri, source_sig, byte_offset, record_count,"
                "  state, error, started_at, updated_at)"
                " VALUES (source.cp_key, source.index_name, source.source_uri, source.source_sig,"
                " source.byte_offset, source.record_count, source.state, source.error,"
                " source.started_at, source.updated_at);",
                (
                    checkpoint.cp_key,
                    checkpoint.index_name,
                    checkpoint.source_uri,
                    checkpoint.source_sig,
                    checkpoint.byte_offset,
                    checkpoint.record_count,
                    checkpoint.state,
                    checkpoint.error,
                    checkpoint.started_at,
                    checkpoint.updated_at,
                ),
            )

    def load_checkpoint(self, cp_key: str) -> Checkpoint | None:
        conn = self._require_conn()
        row = conn.execute(
            f"SELECT * FROM {SCHEMA}.ingest_checkpoints WHERE cp_key = ?", (cp_key,)
        ).fetchone()
        return None if row is None else _row_to_checkpoint(row)

    def list_checkpoints(self, index_name: str | None = None) -> list[Checkpoint]:
        conn = self._require_conn()
        if index_name is None:
            rows = conn.execute(
                f"SELECT * FROM {SCHEMA}.ingest_checkpoints ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {SCHEMA}.ingest_checkpoints"
                " WHERE index_name = ? ORDER BY updated_at DESC",
                (index_name,),
            ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    def delete_checkpoint(self, cp_key: str) -> None:
        conn = self._require_conn()
        with self.transaction():
            conn.execute(f"DELETE FROM {SCHEMA}.ingest_checkpoints WHERE cp_key = ?", (cp_key,))


def _prefix_upper_bound(prefix: str) -> str | None:
    """Return the smallest string that sorts after every string starting with ``prefix``."""
    chars = list(prefix)
    while chars:
        code = ord(chars[-1])
        if code < 0x10FFFF:
            chars[-1] = chr(code + 1)
            return "".join(chars)
        chars.pop()
    return None
