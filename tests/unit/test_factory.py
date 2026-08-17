"""DSN scheme dispatch, registration, and the built-in scheme stubs."""

from __future__ import annotations

from pathlib import Path

import pytest

from scopiengine.errors import BackendNotAvailableError, ConfigurationError
from scopiengine.storage import factory
from scopiengine.storage.base import StorageBackend
from scopiengine.storage.factory import available_schemes, open_storage, register_backend
from scopiengine.storage.sqlite_backend import SQLiteBackend


def _make_stub_backend_class() -> type[StorageBackend]:
    """Build a minimal concrete subclass of StorageBackend for registration tests."""

    def _noop(self: StorageBackend, *args: object, **kwargs: object) -> None:
        return None

    namespace = dict.fromkeys(StorageBackend.__abstractmethods__, _noop)
    namespace["schema_version"] = property(lambda self: 1)
    return type("_StubBackend", (StorageBackend,), namespace)


def test_sqlite_scheme_opens_a_sqlite_backend(tmp_path: Path) -> None:
    dsn = f"sqlite:///{tmp_path / 'scopi.db'}"
    backend = open_storage(dsn)
    assert isinstance(backend, SQLiteBackend)


def test_sqlite_in_memory_dsn_is_accepted() -> None:
    backend = open_storage("sqlite:///:memory:")
    assert isinstance(backend, SQLiteBackend)
    with backend:
        backend.migrate()
        assert backend.ping()


def test_sqlite_relative_dsn_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "scopi.db"
    assert not target.parent.exists()
    open_storage(f"sqlite:///{target}")
    assert target.parent.exists()


def test_mssql_scheme_is_registered_but_unavailable_without_pyodbc() -> None:
    backend = open_storage("mssql://user:pw@dbhost:1433/scopi?driver=ODBC+Driver+18+for+SQL+Server")
    with pytest.raises(BackendNotAvailableError):
        backend.open()


def test_mongodb_scheme_reports_not_available_yet() -> None:
    with pytest.raises(BackendNotAvailableError, match=r"1\.1"):
        open_storage("mongodb://localhost/scopi")


def test_unknown_scheme_lists_available_schemes() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        open_storage("redis://localhost/0")
    message = str(exc_info.value)
    assert "redis" in message
    for scheme in available_schemes():
        assert scheme in message


def test_available_schemes_includes_the_built_ins() -> None:
    schemes = available_schemes()
    assert "sqlite" in schemes
    assert "mssql" in schemes
    assert "mongodb" in schemes


def test_register_backend_adds_a_new_scheme() -> None:
    stub_cls = _make_stub_backend_class()
    register_backend("fake", lambda dsn, options: stub_cls())
    try:
        backend = open_storage("fake://anything")
        assert isinstance(backend, stub_cls)
        assert "fake" in available_schemes()
    finally:
        del factory._REGISTRY["fake"]


def test_malformed_sqlite_dsn_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="malformed sqlite DSN"):
        open_storage("sqlite://relative/path.db")


def test_mssql_dsn_without_host_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="missing host"):
        open_storage("mssql:///scopi")


def test_mssql_dsn_without_database_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="missing database"):
        open_storage("mssql://host:1433/")
