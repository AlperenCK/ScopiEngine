"""One conformance suite, run against every storage backend.

SQLite always runs. MS SQL Server only runs when ``SCOPI_TEST_MSSQL_DSN`` points
at a reachable server; otherwise it is skipped, and it is always marked
``mssql`` so ``pytest -m "not mssql"`` excludes it outright.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scopiengine.errors import (
    IndexAlreadyExistsError,
    IndexNotFoundError,
    UIAccountAlreadyExistsError,
)
from scopiengine.storage import open_storage
from scopiengine.storage.base import StorageBackend
from scopiengine.storage.codec import encode_norms, encode_postings
from scopiengine.storage.models import Checkpoint, NormsBlock, UIAccount, UISession

_BACKEND_PARAMS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param("mssql", id="mssql", marks=pytest.mark.mssql),
]


def reset_backend(backend: StorageBackend) -> None:
    """Return a migrated backend to the state a freshly created one would be in.

    SQLite gets a brand new file per test, but a server-backed backend shares one
    database across the whole suite, so it has to be emptied explicitly. Without
    this a test asserting on ``list_indices()`` sees the previous test's leftovers,
    and re-creating a fixture index raises
    :class:`~scopiengine.errors.IndexAlreadyExistsError`.
    """
    for info in backend.list_indices():
        backend.delete_index(info.name)
    for checkpoint in backend.list_checkpoints():
        backend.delete_checkpoint(checkpoint.cp_key)
    for account in backend.list_ui_accounts():
        backend.delete_ui_account(account.username)


@pytest.fixture(params=_BACKEND_PARAMS)
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StorageBackend]:
    """A migrated, open and empty backend of each kind under test."""
    kind = request.param
    if kind == "mssql":
        dsn = os.environ.get("SCOPI_TEST_MSSQL_DSN")
        if not dsn:
            pytest.skip("SCOPI_TEST_MSSQL_DSN is not set")
    else:
        dsn = f"sqlite:///{tmp_path / 'scopi.db'}"
    with open_storage(dsn) as opened:
        opened.migrate()
        # Empty on the way in as well as out, so a test that fails midway cannot
        # poison the one after it.
        reset_backend(opened)
        try:
            yield opened
        finally:
            reset_backend(opened)


def _create_index(backend: StorageBackend, name: str = "logs") -> None:
    backend.create_index(
        name,
        mapping={"properties": {"message": {"type": "text"}}},
        settings={"number_of_shards": 1},
    )


# -- indices -----------------------------------------------------------------


def test_create_get_delete_list_indices(backend: StorageBackend) -> None:
    assert backend.list_indices() == []
    _create_index(backend, "logs")
    _create_index(backend, "metrics")

    names = [info.name for info in backend.list_indices()]
    assert names == ["logs", "metrics"]

    fetched = backend.get_index("logs")
    assert fetched.name == "logs"
    assert fetched.mapping == {"properties": {"message": {"type": "text"}}}
    assert fetched.doc_count == 0

    backend.delete_index("logs")
    assert [info.name for info in backend.list_indices()] == ["metrics"]


def test_duplicate_create_raises(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    with pytest.raises(IndexAlreadyExistsError):
        _create_index(backend, "logs")


def test_missing_index_raises_on_every_accessor(backend: StorageBackend) -> None:
    with pytest.raises(IndexNotFoundError):
        backend.get_index("absent")
    with pytest.raises(IndexNotFoundError):
        backend.delete_index("absent")
    with pytest.raises(IndexNotFoundError):
        backend.allocate_ords("absent", 1)
    with pytest.raises(IndexNotFoundError):
        backend.put_documents("absent", [])  # not a no-op: the index must exist
    with pytest.raises(IndexNotFoundError):
        backend.get_documents("absent", [0])


def test_update_mapping(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    updated = backend.update_mapping("logs", {"properties": {"level": {"type": "keyword"}}})
    assert updated.mapping == {"properties": {"level": {"type": "keyword"}}}
    assert backend.get_index("logs").mapping == updated.mapping


# -- ordinal and segment allocation -------------------------------------------


def test_ordinal_allocation_is_monotonic_and_non_overlapping(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    first = backend.allocate_ords("logs", 10)
    second = backend.allocate_ords("logs", 5)
    third = backend.allocate_ords("logs", 1)
    assert first == 0
    assert second == 10
    assert third == 15


def test_segment_allocation_is_monotonic(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    assert backend.allocate_segment("logs") == 0
    assert backend.allocate_segment("logs") == 1
    assert backend.allocate_segment("logs") == 2


def test_field_id_resolution_is_stable_and_assigns_new_ids(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    first = backend.resolve_field_ids("logs", ["message", "level"])
    assert set(first) == {"message", "level"}
    assert first["message"] != first["level"]

    again = backend.resolve_field_ids("logs", ["message", "host"])
    assert again["message"] == first["message"]
    assert again["host"] not in first.values()


# -- documents -----------------------------------------------------------------


def test_document_put_get_resolve_delete(backend: StorageBackend) -> None:
    from scopiengine.storage.models import StoredDoc

    _create_index(backend, "logs")
    base = backend.allocate_ords("logs", 3)
    docs = [
        StoredDoc(doc_ord=base, doc_id="a", source=b'{"n": 1}'),
        StoredDoc(doc_ord=base + 1, doc_id="b", source=b'{"n": 2}'),
        StoredDoc(doc_ord=base + 2, doc_id="c", source=b'{"n": 3}'),
    ]
    backend.put_documents("logs", docs)
    assert backend.get_index("logs").doc_count == 3

    fetched = backend.get_documents("logs", [base, base + 2, base + 999])
    assert [d.doc_id for d in fetched] == ["a", "c"]
    assert fetched[0].source == b'{"n": 1}'

    resolved = backend.resolve_ids("logs", ["a", "c", "missing"])
    assert resolved == {"a": base, "c": base + 2}

    assert list(backend.iter_deleted("logs")) == []
    backend.mark_deleted("logs", [base, base + 1])
    assert sorted(backend.iter_deleted("logs")) == [base, base + 1]

    stats = backend.index_stats("logs")
    assert stats["doc_count"] == 3
    assert stats["deleted_count"] == 2
    assert stats["live_doc_count"] == 1


def test_put_documents_overwrites_by_ordinal_without_double_counting(
    backend: StorageBackend,
) -> None:
    from scopiengine.storage.models import StoredDoc

    _create_index(backend, "logs")
    backend.put_documents("logs", [StoredDoc(doc_ord=0, doc_id="a", source=b"1")])
    backend.put_documents("logs", [StoredDoc(doc_ord=0, doc_id="a", source=b"2")])
    assert backend.get_index("logs").doc_count == 1
    assert backend.get_documents("logs", [0])[0].source == b"2"


# -- segments and postings -----------------------------------------------------


def test_segment_write_then_postings_read_back_byte_identical(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    field_ids = backend.resolve_field_ids("logs", ["message"])
    field_id = field_ids["message"]
    segment_id = backend.allocate_segment("logs")

    blob_fast = encode_postings([(0, 2, ()), (3, 1, ())], with_positions=False)
    blob_slow = encode_postings([(1, 1, ())], with_positions=False)
    norms = [
        NormsBlock(
            field_id=field_id,
            segment_id=segment_id,
            base_ord=0,
            doc_count=4,
            total_length=10,
            norms=encode_norms([2, 1, 0, 3]),
        )
    ]
    backend.write_segment(
        "logs",
        segment_id,
        base_ord=0,
        doc_count=4,
        postings_iter=[
            (field_id, "fast", blob_fast, 2, 3),
            (field_id, "slow", blob_slow, 1, 1),
        ],
        norms=norms,
    )

    fetched = backend.get_postings("logs", field_id, "fast")
    assert len(fetched) == 1
    got_segment_id, base_ord, got_blob = fetched[0]
    assert got_segment_id == segment_id
    assert base_ord == 0
    assert got_blob == blob_fast

    assert backend.get_postings("logs", field_id, "absent") == []

    fetched_norms = backend.get_norms("logs", field_id, segment_id)
    assert fetched_norms is not None
    assert fetched_norms.norms == encode_norms([2, 1, 0, 3])
    assert fetched_norms.total_length == 10
    assert backend.get_norms("logs", field_id, segment_id + 1) is None


def test_list_and_drop_segments(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    field_id = backend.resolve_field_ids("logs", ["message"])["message"]
    seg0 = backend.allocate_segment("logs")
    seg1 = backend.allocate_segment("logs")
    for seg in (seg0, seg1):
        backend.write_segment(
            "logs",
            seg,
            base_ord=seg * 10,
            doc_count=1,
            postings_iter=[
                (field_id, "x", encode_postings([(0, 1, ())], with_positions=False), 1, 1)
            ],
            norms=[],
        )

    segments = backend.list_segments("logs")
    assert [s.segment_id for s in segments] == [seg0, seg1]

    backend.drop_segments("logs", [seg0])
    remaining = backend.list_segments("logs")
    assert [s.segment_id for s in remaining] == [seg1]
    assert backend.get_postings("logs", field_id, "x") == [
        (seg1, seg1 * 10, encode_postings([(0, 1, ())], with_positions=False))
    ]


def test_iter_terms_ordering_prefix_and_range(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    field_id = backend.resolve_field_ids("logs", ["message"])["message"]
    segment_id = backend.allocate_segment("logs")
    terms = ["apple", "apricot", "banana", "berry", "cherry"]
    blob = encode_postings([(0, 1, ())], with_positions=False)
    backend.write_segment(
        "logs",
        segment_id,
        base_ord=0,
        doc_count=1,
        postings_iter=[(field_id, term, blob, 1, 1) for term in terms],
        norms=[],
    )

    assert list(backend.iter_terms("logs", field_id)) == sorted(terms)
    assert list(backend.iter_terms("logs", field_id, prefix="ap")) == ["apple", "apricot"]
    assert list(backend.iter_terms("logs", field_id, prefix="b")) == ["banana", "berry"]
    assert list(backend.iter_terms("logs", field_id, lo="banana", hi="cherry")) == [
        "banana",
        "berry",
    ]
    assert list(backend.iter_terms("logs", field_id, prefix="zzz")) == []


def test_term_stats_aggregate_across_segments(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    field_id = backend.resolve_field_ids("logs", ["message"])["message"]

    seg0 = backend.allocate_segment("logs")
    backend.write_segment(
        "logs",
        seg0,
        base_ord=0,
        doc_count=2,
        postings_iter=[
            (field_id, "error", encode_postings([(0, 3, ())], with_positions=False), 1, 3)
        ],
        norms=[],
    )
    seg1 = backend.allocate_segment("logs")
    backend.write_segment(
        "logs",
        seg1,
        base_ord=2,
        doc_count=2,
        postings_iter=[
            (
                field_id,
                "error",
                encode_postings([(0, 1, ()), (1, 2, ())], with_positions=False),
                2,
                3,
            )
        ],
        norms=[],
    )

    stats = backend.get_term_stats("logs", field_id, "error")
    assert stats.doc_freq == 3
    assert stats.total_tf == 6

    absent = backend.get_term_stats("logs", field_id, "never-seen")
    assert absent.doc_freq == 0
    assert absent.total_tf == 0


# -- checkpoints -----------------------------------------------------------------


def _checkpoint(cp_key: str, index_name: str = "logs") -> Checkpoint:
    return Checkpoint(
        cp_key=cp_key,
        index_name=index_name,
        source_uri="/var/log/app.log",
        source_sig="size=1000;mtime=123",
        byte_offset=0,
        record_count=0,
        state="running",
        error=None,
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_checkpoint_save_load_list_delete(backend: StorageBackend) -> None:
    assert backend.load_checkpoint("missing") is None

    cp = _checkpoint("logs::app.log")
    backend.save_checkpoint(cp)
    loaded = backend.load_checkpoint("logs::app.log")
    assert loaded is not None
    assert loaded.byte_offset == 0
    assert loaded.state == "running"

    updated = Checkpoint(
        cp_key=cp.cp_key,
        index_name=cp.index_name,
        source_uri=cp.source_uri,
        source_sig=cp.source_sig,
        byte_offset=4096,
        record_count=50,
        state="done",
        error=None,
        started_at=cp.started_at,
        updated_at="2026-01-01T00:05:00+00:00",
    )
    backend.save_checkpoint(updated)
    reloaded = backend.load_checkpoint(cp.cp_key)
    assert reloaded is not None
    assert reloaded.byte_offset == 4096
    assert reloaded.state == "done"
    assert reloaded.started_at == cp.started_at  # preserved across the upsert

    other = _checkpoint("metrics::m.log", index_name="metrics")
    backend.save_checkpoint(other)
    assert {c.cp_key for c in backend.list_checkpoints()} == {cp.cp_key, other.cp_key}
    assert [c.cp_key for c in backend.list_checkpoints(index_name="metrics")] == [other.cp_key]

    backend.delete_checkpoint(cp.cp_key)
    assert backend.load_checkpoint(cp.cp_key) is None
    backend.delete_checkpoint("never-existed")  # no-op, must not raise


# -- web UI accounts and sessions -------------------------------------------------


def _ui_account(username: str, *, disabled: bool = False) -> UIAccount:
    return UIAccount(
        username=username,
        password_hash="pbkdf2_sha256$1$deadbeef$deadbeef",
        disabled=disabled,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_ui_account_create_get_list_delete(backend: StorageBackend) -> None:
    assert backend.get_ui_account("missing") is None

    backend.create_ui_account(_ui_account("alice"))
    loaded = backend.get_ui_account("alice")
    assert loaded is not None
    assert loaded.username == "alice"
    assert loaded.disabled is False

    backend.create_ui_account(_ui_account("bob"))
    assert {a.username for a in backend.list_ui_accounts()} == {"alice", "bob"}

    assert backend.delete_ui_account("alice") is True
    assert backend.get_ui_account("alice") is None
    assert backend.delete_ui_account("alice") is False  # already gone, no-op


def test_ui_account_create_rejects_a_duplicate_username(backend: StorageBackend) -> None:
    backend.create_ui_account(_ui_account("alice"))
    with pytest.raises(UIAccountAlreadyExistsError):
        backend.create_ui_account(_ui_account("alice"))


def test_ui_account_disable_enable(backend: StorageBackend) -> None:
    backend.create_ui_account(_ui_account("alice"))
    assert backend.set_ui_account_disabled("alice", True) is True
    assert backend.get_ui_account("alice").disabled is True  # type: ignore[union-attr]
    assert backend.set_ui_account_disabled("alice", False) is True
    assert backend.get_ui_account("alice").disabled is False  # type: ignore[union-attr]
    assert backend.set_ui_account_disabled("missing", True) is False


def _ui_session(session_id_hash: str, principal: str = "alice") -> UISession:
    return UISession(
        session_id_hash=session_id_hash,
        principal=principal,
        auth_method="service_account",
        created_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-01T12:00:00+00:00",
    )


def test_ui_session_create_get_delete(backend: StorageBackend) -> None:
    assert backend.get_ui_session("missing") is None

    backend.create_ui_session(_ui_session("hash-1"))
    loaded = backend.get_ui_session("hash-1")
    assert loaded is not None
    assert loaded.principal == "alice"
    assert loaded.auth_method == "service_account"

    backend.delete_ui_session("hash-1")
    assert backend.get_ui_session("hash-1") is None
    backend.delete_ui_session("never-existed")  # no-op, must not raise


def test_delete_expired_ui_sessions(backend: StorageBackend) -> None:
    backend.create_ui_session(_ui_session("still-valid", "alice"))
    backend.create_ui_session(
        UISession(
            session_id_hash="expired",
            principal="alice",
            auth_method="service_account",
            created_at="2025-01-01T00:00:00+00:00",
            expires_at="2025-01-01T01:00:00+00:00",
        )
    )
    removed = backend.delete_expired_ui_sessions(now="2026-01-01T00:00:00+00:00")
    assert removed == 1
    assert backend.get_ui_session("expired") is None
    assert backend.get_ui_session("still-valid") is not None


# -- transactions and migration -------------------------------------------------


def test_transaction_rollback_leaves_no_partial_state(backend: StorageBackend) -> None:
    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError), backend.transaction():
        backend.create_index("survives-nothing", mapping={}, settings={})
        raise _BoomError("simulated failure mid-transaction")

    assert backend.list_indices() == []
    with pytest.raises(IndexNotFoundError):
        backend.get_index("survives-nothing")


def test_migrate_is_idempotent(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    backend.migrate()
    backend.migrate()
    assert [info.name for info in backend.list_indices()] == ["logs"]
    assert backend.schema_version >= 1


def test_ping_reports_a_healthy_backend(backend: StorageBackend) -> None:
    assert backend.ping() is True


def test_index_stats_on_an_empty_index(backend: StorageBackend) -> None:
    _create_index(backend, "logs")
    stats = backend.index_stats("logs")
    assert stats["doc_count"] == 0
    assert stats["segment_count"] == 0
    assert stats["deleted_count"] == 0


# -- shared-database isolation ---------------------------------------------------


def test_reset_empties_a_shared_database(tmp_path: Path) -> None:
    """The fixture's reset must fully empty a database that outlives one test.

    SQLite stands in for the server-backed case here: the same file is reopened
    rather than recreated, which is exactly how MS SQL Server sees the suite. If
    this ever regresses, every shared-database run fails on the second test.
    """
    dsn = f"sqlite:///{tmp_path / 'shared.db'}"

    with open_storage(dsn) as first:
        first.migrate()
        _create_index(first, "logs")
        _create_index(first, "metrics")
        first.save_checkpoint(_checkpoint("cp-1"))
        first.create_ui_account(_ui_account("alice"))
        assert len(first.list_indices()) == 2
        assert len(first.list_checkpoints()) == 1
        assert len(first.list_ui_accounts()) == 1
        reset_backend(first)

    with open_storage(dsn) as second:
        second.migrate()
        assert second.list_indices() == []
        assert second.list_checkpoints() == []
        assert second.list_ui_accounts() == []
        # An index name reused after the reset must not collide.
        _create_index(second, "logs")
        assert [info.name for info in second.list_indices()] == ["logs"]
        reset_backend(second)


# -- cross-backend value fidelity ------------------------------------------------


def test_timestamps_round_trip_verbatim(backend: StorageBackend) -> None:
    """Every backend must return the exact timestamp string it was given.

    A native timestamp column drops the UTC offset, so the same checkpoint would
    read back differently depending on the backend. Storing them as text is what
    keeps the backends interchangeable.
    """
    _create_index(backend, "logs")
    stamp = "2026-01-01T00:00:00+00:00"
    backend.save_checkpoint(_checkpoint("cp-stamp"))
    reloaded = backend.load_checkpoint("cp-stamp")
    assert reloaded is not None
    assert reloaded.started_at == stamp
    assert reloaded.updated_at == stamp

    created = backend.get_index("logs").created_at
    assert "T" in created, f"index created_at is not ISO-8601: {created!r}"


# Characters no single-byte encoding can represent, including the dotless i that
# linters flag as ambiguous. That ambiguity is the point: these values only survive
# if the backend binds them as national character data all the way down.
_NA_INDEX = "günlükler"
_NA_DOC_ID = "kayıt-1"  # noqa: RUF001
_NA_SOURCE = '{"mesaj": "bağlantı reddedildi"}'  # noqa: RUF001
_NA_PATH = "/var/log/uygulamalar/günlük.log"
_NA_CP_KEY = "cp-türkçe"


def test_non_ascii_text_survives_the_round_trip(backend: StorageBackend) -> None:
    """Text columns must not be narrowed to a single-byte encoding in transit.

    Index names and log paths are routinely non-ASCII, and a parameter bound as
    varchar rather than nvarchar corrupts them silently rather than failing.
    """
    from scopiengine.storage.models import StoredDoc

    _create_index(backend, _NA_INDEX)
    assert [info.name for info in backend.list_indices()] == [_NA_INDEX]

    backend.put_documents(
        _NA_INDEX,
        [StoredDoc(doc_ord=0, doc_id=_NA_DOC_ID, source=_NA_SOURCE.encode())],
    )
    assert backend.resolve_ids(_NA_INDEX, [_NA_DOC_ID]) == {_NA_DOC_ID: 0}
    assert backend.get_documents(_NA_INDEX, [0])[0].source.decode() == _NA_SOURCE

    backend.save_checkpoint(
        Checkpoint(
            cp_key=_NA_CP_KEY,
            index_name=_NA_INDEX,
            source_uri=_NA_PATH,
            source_sig="dev:1:abc",
            byte_offset=0,
            record_count=0,
            state="running",
            error=None,
            started_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    stored = backend.load_checkpoint(_NA_CP_KEY)
    assert stored is not None
    assert stored.source_uri == _NA_PATH
    assert stored.index_name == _NA_INDEX
