"""Error hierarchy shared by every layer of the engine.

Every error carries an ``error_type`` and an HTTP ``status`` so the REST layer can
render a consistent envelope without a translation table, and the CLI can map an
exception onto a meaningful exit code.
"""

from __future__ import annotations

__all__ = [
    "AuthenticationError",
    "BackendNotAvailableError",
    "ConfigurationError",
    "IndexAlreadyExistsError",
    "IndexNotFoundError",
    "IngestError",
    "InvalidQueryError",
    "MappingError",
    "PluginError",
    "ScopiError",
    "StorageError",
    "UIAccountAlreadyExistsError",
    "UnsupportedFeatureError",
]


class ScopiError(Exception):
    """Base class for every error raised by ScopiEngine.

    Attributes:
        error_type: Stable machine-readable identifier, surfaced in REST responses.
        status: HTTP status code the REST layer should answer with.
    """

    error_type: str = "scopi_error"
    status: int = 500

    def __init__(self, reason: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail: dict[str, object] = detail or {}

    def to_dict(self) -> dict[str, object]:
        """Render the error as the REST envelope body."""
        error: dict[str, object] = {"type": self.error_type, "reason": self.reason}
        if self.detail:
            error.update(self.detail)
        return {"error": error, "status": self.status}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.reason!r})"


class ConfigurationError(ScopiError):
    """Settings are missing, malformed or mutually inconsistent."""

    error_type = "configuration_error"
    status = 500


class StorageError(ScopiError):
    """A storage backend failed to complete an operation."""

    error_type = "storage_error"
    status = 500


class BackendNotAvailableError(StorageError):
    """The requested storage backend exists but cannot be used in this build.

    Raised for a DSN scheme that is registered but unimplemented, or whose optional
    driver dependency is not installed.
    """

    error_type = "backend_not_available"
    status = 501


class IndexNotFoundError(ScopiError):
    """The named index does not exist."""

    error_type = "index_not_found"
    status = 404


class IndexAlreadyExistsError(ScopiError):
    """An index with that name already exists."""

    error_type = "index_already_exists"
    status = 409


class MappingError(ScopiError):
    """A mapping is invalid, or a document conflicts with the existing mapping."""

    error_type = "mapping_error"
    status = 400


class InvalidQueryError(ScopiError):
    """A query could not be parsed or compiled."""

    error_type = "invalid_query"
    status = 400


class UnsupportedFeatureError(InvalidQueryError):
    """A recognised but unimplemented feature was requested.

    Raised rather than silently ignoring input, so a query never returns quietly
    wrong results.
    """

    error_type = "unsupported_feature"
    status = 400


class PluginError(ScopiError):
    """A plugin failed to load or misbehaved during registration."""

    error_type = "plugin_error"
    status = 500


class IngestError(ScopiError):
    """An ingestion pipeline could not start, continue or resume."""

    error_type = "ingest_error"
    status = 400


class AuthenticationError(ScopiError):
    """A login attempt or an existing session failed to authenticate.

    Raised for wrong credentials, a disabled account, or a missing/expired/
    invalid session — deliberately without distinguishing which, in the
    response, so a client cannot use the error to enumerate valid usernames.
    """

    error_type = "authentication_error"
    status = 401


class UIAccountAlreadyExistsError(ScopiError):
    """A web UI service account with that username already exists."""

    error_type = "ui_account_already_exists"
    status = 409
