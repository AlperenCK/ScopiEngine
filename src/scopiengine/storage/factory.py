"""DSN-driven construction of storage backends.

:func:`open_storage` is the one place the rest of the engine touches to get a
:class:`~scopiengine.storage.base.StorageBackend`; it never imports a concrete
backend module until the matching scheme is actually requested, so an unused
backend's optional dependencies (``pyodbc`` for MS SQL) are never required.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from scopiengine.errors import BackendNotAvailableError, ConfigurationError
from scopiengine.storage.base import StorageBackend

__all__ = ["available_schemes", "open_storage", "register_backend"]

#: Builds a backend from a DSN and any extra keyword options.
BackendFactory = Callable[[str, dict[str, object]], StorageBackend]

_REGISTRY: dict[str, BackendFactory] = {}

#: DSN version the mongodb backend is planned to ship in.
_MONGODB_TARGET_VERSION = "1.1"


def _sqlite_path(dsn: str) -> str:
    """Extract the filesystem path (or ``:memory:``) from a ``sqlite://`` DSN."""
    prefix = "sqlite://"
    remainder = dsn[len(prefix) :]
    if not remainder.startswith("/") or len(remainder) < 2:
        raise ConfigurationError(
            f"malformed sqlite DSN {dsn!r}; expected 'sqlite:///path' or 'sqlite:///:memory:'"
        )
    return remainder[1:]


def _sqlite_factory(dsn: str, options: dict[str, object]) -> StorageBackend:
    from pathlib import Path

    from scopiengine.storage.sqlite_backend import SQLiteBackend

    path = _sqlite_path(dsn)
    if path != ":memory:":
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
    return SQLiteBackend(path)


def _mssql_factory(dsn: str, options: dict[str, object]) -> StorageBackend:
    from scopiengine.storage.mssql_backend import MSSQLBackend, MSSQLConfig

    parts = urlsplit(dsn)
    if not parts.hostname:
        raise ConfigurationError(f"malformed mssql DSN {dsn!r}: missing host")
    database = parts.path.lstrip("/")
    if not database:
        raise ConfigurationError(f"malformed mssql DSN {dsn!r}: missing database name")

    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    driver = query.pop("driver", "ODBC Driver 18 for SQL Server")

    config = MSSQLConfig(
        host=parts.hostname,
        port=parts.port or 1433,
        database=database,
        user=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
        driver=driver,
        options=query,
    )
    return MSSQLBackend(config)


def _mongodb_factory(dsn: str, options: dict[str, object]) -> StorageBackend:
    raise BackendNotAvailableError(
        f"the mongodb storage backend is planned for ScopiEngine {_MONGODB_TARGET_VERSION} "
        "and is not available yet",
        detail={"scheme": "mongodb"},
    )


def register_backend(scheme: str, factory: BackendFactory) -> None:
    """Register a factory for a DSN scheme, replacing any existing one.

    Lets the plugin system add new schemes (or replace ``mongodb`` once it
    ships) without changing this module.

    Args:
        scheme: DSN scheme, e.g. ``"sqlite"`` — the part before ``://``.
        factory: Called as ``factory(dsn, options)`` to build the backend.
    """
    _REGISTRY[scheme] = factory


def available_schemes() -> tuple[str, ...]:
    """List every registered DSN scheme, sorted."""
    return tuple(sorted(_REGISTRY))


def open_storage(dsn: str, **options: object) -> StorageBackend:
    """Build a :class:`~scopiengine.storage.base.StorageBackend` from a DSN.

    The backend is constructed but not opened — call :meth:`~scopiengine.storage
    .base.StorageBackend.open` or use it as a context manager::

        with open_storage(dsn) as backend:
            backend.migrate()

    Args:
        dsn: A URI such as ``sqlite:///./data/scopi.db``,
            ``mssql://user:pw@host:1433/scopi?driver=ODBC+Driver+18+for+SQL+Server``,
            or ``sqlite:///:memory:``.
        **options: Extra keyword options forwarded to the backend's factory.

    Returns:
        A backend instance for the DSN's scheme.

    Raises:
        ConfigurationError: The DSN has no recognised scheme, or is malformed.
        BackendNotAvailableError: The scheme is registered but not usable in
            this build (missing optional dependency, or not yet implemented).
    """
    scheme = dsn.split("://", 1)[0] if "://" in dsn else ""
    factory = _REGISTRY.get(scheme)
    if factory is None:
        available = ", ".join(available_schemes())
        raise ConfigurationError(
            f"unknown storage scheme {scheme!r} in DSN {dsn!r}; available schemes: {available}"
        )
    return factory(dsn, options)


register_backend("sqlite", _sqlite_factory)
register_backend("mssql", _mssql_factory)
register_backend("mongodb", _mongodb_factory)
