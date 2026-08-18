"""SQLite implementation of :class:`~scopiengine.storage.base.StorageBackend`.

The zero-configuration default backend: no server, no extra dependency — the
Python standard library's :mod:`sqlite3` module is all it needs. Every write goes
through :meth:`SQLiteBackend.transaction`, and bulk writes use ``executemany``
inside a single transaction rather than one round trip per row.
"""

from __future__ import annotations

import functools
import itertools
import json
import sqlite3
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from scopiengine import __version__
from scopiengine.errors import IndexAlreadyExistsError, IndexNotFoundError, StorageError
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
from scopiengine.storage.sqlite_ddl import MIGRATION_STATEMENTS, PRAGMA_STATEMENTS, SCHEMA_VERSION

__all__ = ["SQLiteBackend"]

#: Rows per ``executemany`` batch when writing a segment's postings, so a segment
#: with millions of terms never requires the whole postings iterator in memory.
_WRITE_CHUNK_SIZE = 1000

_T = TypeVar("_T")
_R = TypeVar("_R")


def _synchronized(method: Callable[..., _R]) -> Callable[..., _R]:
    """Serialize one method call against every other locked call on the same backend.

    ``check_same_thread=False`` (see :meth:`SQLiteBackend.open`) only disables
    Python's same-thread guard — the standard library is explicit that the
    caller is then responsible for its own synchronization. The REST API runs
    its handlers in Starlette's worker threadpool, so two requests genuinely can
    call into the same backend at the same instant, and an unsynchronized
    ``sqlite3.Connection`` shared that way can surface as a driver-level
    ``InterfaceError`` under real concurrency — this is not a hypothetical: it
    is how a pre-existing test's occasional CI failure was root-caused. Every
    method that touches ``self._conn`` acquires ``self._lock`` for its whole
    body; :meth:`SQLiteBackend.transaction` does the same directly in its own
    body, since a ``@contextmanager``'s lock must stay held across its ``yield``
    for the whole duration the caller has the transaction open — a nested
    ``with self.transaction():`` (or a decorated call issued from inside one)
    on the same thread re-enters the same lock without blocking on itself,
    since it is reentrant.
    """

    @functools.wraps(method)
    def wrapper(self: SQLiteBackend, *args: Any, **kwargs: Any) -> _R:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string, second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _chunked(iterable: Iterable[_T], size: int) -> Iterator[list[_T]]:
    """Yield ``iterable`` in lists of at most ``size`` items, without buffering the rest."""
    iterator = iter(iterable)
    while True:
        chunk = list(itertools.islice(iterator, size))
        if not chunk:
            return
        yield chunk


def _prefix_upper_bound(prefix: str) -> str | None:
    """Return the smallest string that sorts after every string starting with ``prefix``.

    Used to turn a prefix query into a half-open range ``[prefix, upper_bound)``.
    Returns ``None`` when ``prefix`` is empty or is made entirely of the maximum
    Unicode code point, in which case there is no finite upper bound.
    """
    chars = list(prefix)
    while chars:
        code = ord(chars[-1])
        if code < 0x10FFFF:
            chars[-1] = chr(code + 1)
            return "".join(chars)
        chars.pop()
    return None


def _row_to_index_info(row: sqlite3.Row) -> IndexInfo:
    return IndexInfo(
        index_id=row["index_id"],
        name=row["name"],
        mapping=json.loads(row["mapping_json"]),
        settings=json.loads(row["settings_json"]),
        doc_count=row["doc_count"],
        next_ord=row["next_ord"],
        next_segment=row["next_segment"],
        created_at=row["created_at"],
    )


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        cp_key=row["cp_key"],
        index_name=row["index_name"],
        source_uri=row["source_uri"],
        source_sig=row["source_sig"],
        byte_offset=row["byte_offset"],
        record_count=row["record_count"],
        state=row["state"],
        error=row["error"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
    )


class SQLiteBackend(StorageBackend):
    """Storage backend backed by a single SQLite database file.

    Args:
        path: Filesystem path to the database, or ``:memory:`` for an in-process,
            non-durable database.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._tx_depth = 0
        #: Guards every access to ``self._conn``. Reentrant so a method that
        #: opens a transaction and then calls another locked method on the
        #: same thread (or a nested ``with self.transaction():``) does not
        #: block on itself.
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @_synchronized
    def open(self) -> None:
        if self._conn is not None:
            return
        if self._path != ":memory:":
            parent = Path(self._path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMA_STATEMENTS:
            conn.execute(pragma)
        self._conn = conn

    @_synchronized
    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @_synchronized
    def ping(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT 1")
        except sqlite3.Error:
            return False
        return True

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("storage backend is not open; call open() first")
        return self._conn

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    def migrate(self) -> None:
        conn = self._require_conn()
        with self.transaction():
            for statement in MIGRATION_STATEMENTS:
                conn.execute(statement)
            existing = conn.execute(
                "SELECT value FROM scopi_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO scopi_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                stored = int(existing["value"])
                if stored > SCHEMA_VERSION:
                    raise StorageError(
                        f"database schema version {stored} is newer than this build of "
                        f"ScopiEngine supports ({SCHEMA_VERSION}); upgrade ScopiEngine to "
                        "open this database"
                    )
            conn.execute(
                "INSERT INTO scopi_meta (key, value) VALUES ('engine_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (__version__,),
            )

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        # Held for the whole scope, including across the yield: the lock must stay
        # taken for as long as the caller has the transaction open, not just for
        # this generator's own synchronous prelude. A decorated method called from
        # inside the with-block (or a nested with self.transaction():) re-enters
        # the same lock on the same thread without blocking on itself.
        with self._lock:
            conn = self._require_conn()
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1
                return

            self._tx_depth = 1
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except sqlite3.Error as exc:
                # A driver-level failure: translate it into the storage error hierarchy.
                conn.execute("ROLLBACK")
                raise StorageError(f"transaction rolled back: {exc}") from exc
            except BaseException:
                # Anything else (ScopiError subclasses, caller-raised exceptions) rolls
                # back but keeps its own type and message intact.
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._tx_depth = 0

    # -- indices -------------------------------------------------------------

    def _get_index_row(self, name: str) -> sqlite3.Row:
        conn = self._require_conn()
        row = conn.execute("SELECT * FROM indices WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise IndexNotFoundError(f"no such index: {name!r}")
        return row

    def create_index(
        self, name: str, mapping: dict[str, Any], settings: dict[str, Any]
    ) -> IndexInfo:
        conn = self._require_conn()
        with self.transaction():
            existing = conn.execute("SELECT 1 FROM indices WHERE name = ?", (name,)).fetchone()
            if existing is not None:
                raise IndexAlreadyExistsError(f"index already exists: {name!r}")
            created_at = _now_iso()
            cur = conn.execute(
                "INSERT INTO indices"
                " (name, mapping_json, settings_json, doc_count, next_ord, next_segment,"
                "  created_at)"
                " VALUES (?, ?, ?, 0, 0, 0, ?)",
                (name, json.dumps(mapping), json.dumps(settings), created_at),
            )
            index_id = cur.lastrowid
        assert index_id is not None
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

    @_synchronized
    def get_index(self, name: str) -> IndexInfo:
        return _row_to_index_info(self._get_index_row(name))

    def delete_index(self, name: str) -> None:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(name)
            index_id = row["index_id"]
            for table in ("fields", "documents", "segments", "terms", "field_norms", "deletions"):
                conn.execute(f"DELETE FROM {table} WHERE index_id = ?", (index_id,))
            conn.execute("DELETE FROM indices WHERE index_id = ?", (index_id,))

    @_synchronized
    def list_indices(self) -> list[IndexInfo]:
        conn = self._require_conn()
        rows = conn.execute("SELECT * FROM indices ORDER BY name").fetchall()
        return [_row_to_index_info(row) for row in rows]

    def update_mapping(self, name: str, mapping: dict[str, Any]) -> IndexInfo:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(name)
            conn.execute(
                "UPDATE indices SET mapping_json = ? WHERE index_id = ?",
                (json.dumps(mapping), row["index_id"]),
            )
        return self.get_index(name)

    # -- ordinal and segment allocation ---------------------------------------

    def allocate_ords(self, index: str, n: int) -> int:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            base = row["next_ord"]
            conn.execute(
                "UPDATE indices SET next_ord = next_ord + ? WHERE index_id = ?",
                (n, row["index_id"]),
            )
        return int(base)

    def allocate_segment(self, index: str) -> int:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            segment_id = row["next_segment"]
            conn.execute(
                "UPDATE indices SET next_segment = next_segment + 1 WHERE index_id = ?",
                (row["index_id"],),
            )
        return int(segment_id)

    def resolve_field_ids(self, index: str, names: Sequence[str]) -> dict[str, int]:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row["index_id"]
            result: dict[str, int] = {}
            missing: list[str] = []
            for name in names:
                found = conn.execute(
                    "SELECT field_id FROM fields WHERE index_id = ? AND name = ?",
                    (index_id, name),
                ).fetchone()
                if found is None:
                    if name not in missing:
                        missing.append(name)
                else:
                    result[name] = found["field_id"]
            if missing:
                next_id_row = conn.execute(
                    "SELECT COALESCE(MAX(field_id), -1) AS m FROM fields WHERE index_id = ?",
                    (index_id,),
                ).fetchone()
                next_id = next_id_row["m"] + 1
                to_insert = []
                for name in missing:
                    result[name] = next_id
                    to_insert.append((index_id, next_id, name))
                    next_id += 1
                conn.executemany(
                    "INSERT INTO fields (index_id, field_id, name) VALUES (?, ?, ?)", to_insert
                )
        return result

    # -- documents -------------------------------------------------------------

    @_synchronized
    def put_documents(self, index: str, docs: Iterable[StoredDoc]) -> None:
        conn = self._require_conn()
        batch = list(docs)
        if not batch:
            self._get_index_row(index)
            return
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row["index_id"]
            ords = [doc.doc_ord for doc in batch]
            placeholders = ",".join("?" * len(ords))
            existing = conn.execute(
                f"SELECT doc_ord FROM documents WHERE index_id = ? AND doc_ord IN ({placeholders})",
                [index_id, *ords],
            ).fetchall()
            new_count = len(batch) - len(existing)
            conn.executemany(
                "INSERT INTO documents (index_id, doc_ord, doc_id, source) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (index_id, doc_ord) DO UPDATE SET"
                " doc_id = excluded.doc_id, source = excluded.source",
                [(index_id, doc.doc_ord, doc.doc_id, doc.source) for doc in batch],
            )
            if new_count:
                conn.execute(
                    "UPDATE indices SET doc_count = doc_count + ? WHERE index_id = ?",
                    (new_count, index_id),
                )

    @_synchronized
    def get_documents(self, index: str, ords: Sequence[int]) -> list[StoredDoc]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        if not ords:
            return []
        index_id = row["index_id"]
        placeholders = ",".join("?" * len(ords))
        rows = conn.execute(
            "SELECT doc_ord, doc_id, source FROM documents"
            f" WHERE index_id = ? AND doc_ord IN ({placeholders})",
            [index_id, *ords],
        ).fetchall()
        by_ord = {r["doc_ord"]: r for r in rows}
        return [
            StoredDoc(doc_ord=o, doc_id=by_ord[o]["doc_id"], source=bytes(by_ord[o]["source"]))
            for o in ords
            if o in by_ord
        ]

    @_synchronized
    def resolve_ids(self, index: str, doc_ids: Sequence[str]) -> dict[str, int]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        if not doc_ids:
            return {}
        index_id = row["index_id"]
        placeholders = ",".join("?" * len(doc_ids))
        rows = conn.execute(
            "SELECT doc_id, doc_ord FROM documents"
            f" WHERE index_id = ? AND doc_id IN ({placeholders})",
            [index_id, *doc_ids],
        ).fetchall()
        return {r["doc_id"]: r["doc_ord"] for r in rows}

    @_synchronized
    def mark_deleted(self, index: str, ords: Sequence[int]) -> None:
        conn = self._require_conn()
        if not ords:
            self._get_index_row(index)
            return
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row["index_id"]
            conn.executemany(
                "INSERT OR IGNORE INTO deletions (index_id, doc_ord) VALUES (?, ?)",
                [(index_id, o) for o in ords],
            )

    def iter_deleted(self, index: str) -> Iterator[int]:
        with self._lock:
            conn = self._require_conn()
            row = self._get_index_row(index)
            cur = conn.execute(
                "SELECT doc_ord FROM deletions WHERE index_id = ? ORDER BY doc_ord",
                (row["index_id"],),
            )
        return self._locked_rows(cur, lambda r: r["doc_ord"])

    def _locked_rows(
        self, cur: sqlite3.Cursor, transform: Callable[[sqlite3.Row], _T]
    ) -> Iterator[_T]:
        """Pull rows from ``cur`` one at a time, each fetch its own critical section.

        ``iter_deleted``/``iter_terms`` stay genuinely lazy on purpose — that is
        what keeps a term matching millions of documents from ever materialising a
        list — but the cursor they hand back is consumed long after the method that
        created it returns, on whatever thread the caller happens to iterate from.
        Locking only the individual ``next(cur)`` call, rather than the method body
        that created the cursor, protects each fetch against concurrent access
        without holding the lock across the caller's own processing between rows.
        """
        while True:
            with self._lock:
                try:
                    row = next(cur)
                except StopIteration:
                    return
            yield transform(row)

    # -- segments and postings -------------------------------------------------

    @_synchronized
    def write_segment(
        self,
        index: str,
        segment_id: int,
        base_ord: int,
        doc_count: int,
        postings_iter: Iterable[SegmentTermEntry],
        norms: Iterable[NormsBlock],
    ) -> None:
        conn = self._require_conn()
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row["index_id"]
            conn.execute(
                "INSERT INTO segments"
                " (index_id, segment_id, base_ord, doc_count, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (index_id, segment_id, base_ord, doc_count, int(SegmentState.LIVE), _now_iso()),
            )
            for chunk in _chunked(postings_iter, _WRITE_CHUNK_SIZE):
                conn.executemany(
                    "INSERT INTO terms"
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
                conn.executemany(
                    "INSERT INTO field_norms"
                    " (index_id, field_id, segment_id, base_ord, doc_count, total_length, norms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    norms_rows,
                )

    @_synchronized
    def list_segments(self, index: str) -> list[SegmentInfo]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        rows = conn.execute(
            "SELECT * FROM segments WHERE index_id = ? ORDER BY base_ord",
            (row["index_id"],),
        ).fetchall()
        return [
            SegmentInfo(
                segment_id=r["segment_id"],
                base_ord=r["base_ord"],
                doc_count=r["doc_count"],
                state=SegmentState(r["state"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    @_synchronized
    def drop_segments(self, index: str, segment_ids: Sequence[int]) -> None:
        conn = self._require_conn()
        if not segment_ids:
            self._get_index_row(index)
            return
        with self.transaction():
            row = self._get_index_row(index)
            index_id = row["index_id"]
            placeholders = ",".join("?" * len(segment_ids))
            for table in ("segments", "terms", "field_norms"):
                conn.execute(
                    f"DELETE FROM {table} WHERE index_id = ? AND segment_id IN ({placeholders})",
                    [index_id, *segment_ids],
                )

    @_synchronized
    def get_postings(self, index: str, field_id: int, term: str) -> list[tuple[int, int, bytes]]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        rows = conn.execute(
            "SELECT t.segment_id AS segment_id, s.base_ord AS base_ord, t.postings AS postings"
            " FROM terms t JOIN segments s"
            " ON s.index_id = t.index_id AND s.segment_id = t.segment_id"
            " WHERE t.index_id = ? AND t.field_id = ? AND t.term = ? AND s.state != ?",
            (row["index_id"], field_id, term, int(SegmentState.DEAD)),
        ).fetchall()
        return [(r["segment_id"], r["base_ord"], bytes(r["postings"])) for r in rows]

    def iter_terms(
        self,
        index: str,
        field_id: int,
        *,
        prefix: str | None = None,
        lo: str | None = None,
        hi: str | None = None,
    ) -> Iterator[str]:
        with self._lock:
            conn = self._require_conn()
            row = self._get_index_row(index)
            index_id = row["index_id"]

            lower: str | None
            upper: str | None
            if prefix is not None:
                lower, upper = prefix, _prefix_upper_bound(prefix)
            else:
                lower, upper = lo, hi

            query = "SELECT DISTINCT term FROM terms WHERE index_id = ? AND field_id = ?"
            params: list[Any] = [index_id, field_id]
            if lower is not None:
                query += " AND term >= ?"
                params.append(lower)
            if upper is not None:
                query += " AND term < ?"
                params.append(upper)
            query += " ORDER BY term"

            cur = conn.execute(query, params)
        return self._locked_rows(cur, lambda r: r["term"])

    @_synchronized
    def get_term_stats(self, index: str, field_id: int, term: str) -> TermStats:
        conn = self._require_conn()
        row = self._get_index_row(index)
        stats = conn.execute(
            "SELECT COALESCE(SUM(doc_freq), 0) AS doc_freq, COALESCE(SUM(total_tf), 0) AS total_tf"
            " FROM terms t JOIN segments s"
            " ON s.index_id = t.index_id AND s.segment_id = t.segment_id"
            " WHERE t.index_id = ? AND t.field_id = ? AND t.term = ? AND s.state != ?",
            (row["index_id"], field_id, term, int(SegmentState.DEAD)),
        ).fetchone()
        return TermStats(term=term, doc_freq=stats["doc_freq"], total_tf=stats["total_tf"])

    @_synchronized
    def get_norms(self, index: str, field_id: int, segment_id: int) -> NormsBlock | None:
        conn = self._require_conn()
        row = self._get_index_row(index)
        found = conn.execute(
            "SELECT base_ord, doc_count, total_length, norms FROM field_norms"
            " WHERE index_id = ? AND field_id = ? AND segment_id = ?",
            (row["index_id"], field_id, segment_id),
        ).fetchone()
        if found is None:
            return None
        return NormsBlock(
            field_id=field_id,
            segment_id=segment_id,
            base_ord=found["base_ord"],
            doc_count=found["doc_count"],
            total_length=found["total_length"],
            norms=bytes(found["norms"]),
        )

    @_synchronized
    def index_stats(self, index: str) -> dict[str, Any]:
        conn = self._require_conn()
        row = self._get_index_row(index)
        index_id = row["index_id"]
        deleted = conn.execute(
            "SELECT COUNT(*) AS n FROM deletions WHERE index_id = ?", (index_id,)
        ).fetchone()["n"]
        segment_count = conn.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE index_id = ?", (index_id,)
        ).fetchone()["n"]
        return {
            "index": index,
            "doc_count": row["doc_count"],
            "deleted_count": deleted,
            "live_doc_count": max(row["doc_count"] - deleted, 0),
            "segment_count": segment_count,
            "next_ord": row["next_ord"],
            "next_segment": row["next_segment"],
        }

    # -- ingestion checkpoints ---------------------------------------------

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        conn = self._require_conn()
        with self.transaction():
            conn.execute(
                "INSERT INTO ingest_checkpoints"
                " (cp_key, index_name, source_uri, source_sig, byte_offset, record_count,"
                "  state, error, started_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (cp_key) DO UPDATE SET"
                " index_name = excluded.index_name, source_uri = excluded.source_uri,"
                " source_sig = excluded.source_sig, byte_offset = excluded.byte_offset,"
                " record_count = excluded.record_count, state = excluded.state,"
                " error = excluded.error, updated_at = excluded.updated_at",
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

    @_synchronized
    def load_checkpoint(self, cp_key: str) -> Checkpoint | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM ingest_checkpoints WHERE cp_key = ?", (cp_key,)
        ).fetchone()
        return None if row is None else _row_to_checkpoint(row)

    @_synchronized
    def list_checkpoints(self, index_name: str | None = None) -> list[Checkpoint]:
        conn = self._require_conn()
        if index_name is None:
            rows = conn.execute(
                "SELECT * FROM ingest_checkpoints ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ingest_checkpoints WHERE index_name = ? ORDER BY updated_at DESC",
                (index_name,),
            ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    def delete_checkpoint(self, cp_key: str) -> None:
        conn = self._require_conn()
        with self.transaction():
            conn.execute("DELETE FROM ingest_checkpoints WHERE cp_key = ?", (cp_key,))
