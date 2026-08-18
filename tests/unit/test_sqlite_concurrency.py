"""``SQLiteBackend`` must survive genuinely concurrent use from multiple threads.

The REST API runs its handlers in Starlette's worker threadpool (see
``docs/USAGE.md``'s "The REST API" section), so two HTTP requests can call into
the same backend at the same instant — this is not a hypothetical, it is the
documented, intended way the server is used. A single shared
``sqlite3.Connection`` opened with ``check_same_thread=False`` is only safe for
that if every access is externally serialized; without it, two threads issuing
``BEGIN``/``COMMIT`` and reads on the same connection at once corrupt the
connection's transaction state.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from scopiengine.storage.models import StoredDoc
from scopiengine.storage.sqlite_backend import SQLiteBackend


def test_concurrent_writers_and_readers_do_not_corrupt_the_connection(
    tmp_path: Path,
) -> None:
    """Three threads run full write transactions while three more read continuously.

    Before the backend serialized access to its shared connection, this reliably
    reproduced driver-level failures within a couple of seconds ("cannot start a
    transaction within a transaction", "cannot commit - no transaction is
    active") and lost writes. The fixed backend must run this workload with zero
    errors and a final document count that exactly matches what the writers
    report having written — no corruption, no silently dropped batches.
    """
    backend = SQLiteBackend(str(tmp_path / "stress.db"))
    backend.open()
    try:
        backend.migrate()
        backend.create_index("t", mapping={"properties": {}}, settings={})

        errors: list[tuple[str, str]] = []
        errors_lock = threading.Lock()
        docs_written = 0
        docs_written_lock = threading.Lock()
        stop = threading.Event()

        def writer() -> None:
            nonlocal docs_written
            while not stop.is_set():
                try:
                    base = backend.allocate_ords("t", 5)
                    with backend.transaction():
                        backend.put_documents(
                            "t",
                            [
                                StoredDoc(doc_ord=base + i, doc_id=f"d{base + i}", source=b"{}")
                                for i in range(5)
                            ],
                        )
                    with docs_written_lock:
                        docs_written += 5
                except Exception as exc:
                    with errors_lock:
                        errors.append((type(exc).__name__, str(exc)))
                    return

        def reader() -> None:
            while not stop.is_set():
                try:
                    backend.get_index("t")
                    backend.list_indices()
                    backend.index_stats("t")
                except Exception as exc:
                    with errors_lock:
                        errors.append((type(exc).__name__, str(exc)))
                    return

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert backend.get_index("t").doc_count == docs_written
    finally:
        backend.close()
