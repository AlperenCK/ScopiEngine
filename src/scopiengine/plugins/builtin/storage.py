"""Registers the built-in storage DSN schemes.

:mod:`scopiengine.storage.factory` already registers ``sqlite``, ``mssql`` and
``mongodb`` at import time — a DSN must resolve to a backend even in code that
never touches the plugin system at all (the storage conformance tests, for
one). This module re-registers the same schemes through the ``storage_backend``
hook so that path is genuinely exercised, and so a third-party plugin adding a
new DSN scheme (say, ``postgres://``) has a real, first-party example to model
itself on rather than a stub.
"""

from __future__ import annotations

from collections.abc import Callable

from scopiengine.storage.factory import (
    BackendFactory,
    _mongodb_factory,
    _mssql_factory,
    _sqlite_factory,
)

__all__ = ["register_storage_backends"]


def register_storage_backends(register: Callable[[str, BackendFactory], None]) -> None:
    """Register the ``sqlite://``, ``mssql://`` and ``mongodb://`` schemes."""
    register("sqlite", _sqlite_factory)
    register("mssql", _mssql_factory)
    register("mongodb", _mongodb_factory)
