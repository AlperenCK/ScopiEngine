"""Runtime configuration.

Settings resolve from four layers, later layers winning: built-in defaults, a JSON
config file, ``SCOPI_*`` environment variables, then explicit overrides — which is
how the CLI applies command-line options.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from scopiengine import DEFAULT_PORT
from scopiengine.errors import ConfigurationError

__all__ = ["DEFAULT_STORAGE_DSN", "ENV_PREFIX", "Settings", "load_settings"]

#: Prefix for environment variables, e.g. ``SCOPI_STORAGE`` maps to ``storage``.
ENV_PREFIX = "SCOPI_"

#: Zero-configuration default: a SQLite database beneath the working directory.
DEFAULT_STORAGE_DSN = "sqlite:///./data/scopi.db"

#: Declared type of every setting, used to coerce strings from the environment.
_TYPES: dict[str, type] = {
    "storage": str,
    "host": str,
    "port": int,
    "log_level": str,
    "log_format": str,
    "plugins": tuple,
    "strict_plugins": bool,
    "buffer_mb": int,
    "batch_size": int,
    "batch_bytes": int,
    "flush_interval": float,
    "queue_size": int,
    "max_segments": int,
    "max_sort_candidates": int,
    "ui_auth_enabled": bool,
    "ui_session_ttl": int,
}


@dataclass(slots=True)
class Settings:
    """Resolved engine configuration.

    Attributes:
        storage: Storage DSN selecting and configuring the backend.
        host: Interface ``scopi serve`` binds to.
        port: TCP port ``scopi serve`` binds to.
        log_level: Root log level name.
        log_format: ``text`` for human-readable output, ``json`` for structured logs.
        plugins: Extra module paths to import as plugins, on top of entry-point discovery.
        strict_plugins: Fail startup when a plugin raises, instead of skipping it.
        buffer_mb: In-memory index buffer size before a segment is flushed.
        batch_size: Documents per ingestion batch.
        batch_bytes: Approximate bytes per ingestion batch.
        flush_interval: Seconds before a partial ingestion batch is flushed anyway.
        queue_size: Bounded queue depth between the ingest reader and the indexer.
        max_segments: Live segment count that triggers an automatic merge.
        max_sort_candidates: Maximum number of matches a field-sorted search
            (``| sort <field>`` in ScopiQL, or the DSL's ``sort``) examines
            while looking for the true index-wide top ``from_ + size``. Past
            this many matches, the sort stops examining further candidates —
            ``total`` stays the true, complete match count regardless, but the
            page itself is drawn only from the examined candidates, and the
            response's ``scopi.sort_truncated`` flag is set so a truncated
            sort is never indistinguishable from a complete one. A pure
            ``_score`` sort (the default, or no ``sort`` at all) never
            consults this setting — it is already exact and unbounded, since
            relevance ranking is computed inline while matching.
        ui_auth_enabled: Require a login for the web UI (``/_ui/``). Off by
            default — installations that upgrade keep the UI reachable
            exactly as before; turning this on with no
            :class:`~scopiengine.storage.models.UIAccount` yet created locks
            the UI out until one is bootstrapped with ``scopi ui-account
            create``. The REST API itself is never gated by this setting.
        ui_session_ttl: Seconds a web UI login stays valid before it must be
            re-established.
    """

    storage: str = DEFAULT_STORAGE_DSN
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    log_format: str = "text"
    plugins: tuple[str, ...] = ()
    strict_plugins: bool = False
    buffer_mb: int = 64
    batch_size: int = 5000
    batch_bytes: int = 8 * 1024 * 1024
    flush_interval: float = 2.0
    queue_size: int = 8
    max_segments: int = 10
    max_sort_candidates: int = 10000
    ui_auth_enabled: bool = False
    ui_session_ttl: int = 43200

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Reject values that would only misbehave far away from their origin."""
        if not self.storage:
            raise ConfigurationError("storage DSN must not be empty")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError(f"port must be between 1 and 65535, got {self.port}")
        if self.log_format not in ("text", "json"):
            raise ConfigurationError(
                f"log_format must be 'text' or 'json', got {self.log_format!r}"
            )
        for name in (
            "buffer_mb",
            "batch_size",
            "batch_bytes",
            "queue_size",
            "max_segments",
            "max_sort_candidates",
            "ui_session_ttl",
        ):
            value = getattr(self, name)
            if value < 1:
                raise ConfigurationError(f"{name} must be >= 1, got {value}")
        if self.flush_interval <= 0:
            raise ConfigurationError(f"flush_interval must be > 0, got {self.flush_interval}")

    def to_dict(self) -> dict[str, Any]:
        """Return the settings as a plain dictionary, safe to print or serialise."""
        data = asdict(self)
        data["plugins"] = list(self.plugins)
        return data


def _coerce(name: str, raw: str, target: type) -> Any:
    """Convert an environment string to the type declared for ``name``."""
    source = f"{ENV_PREFIX}{name.upper()}"
    if target is bool:
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigurationError(f"{source} must be a boolean, got {raw!r}")
    if target is tuple:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if target in (int, float):
        try:
            return target(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{source} must be a {target.__name__}, got {raw!r}") from exc
    return raw


def _load_config_file(path: Path, known: set[str]) -> dict[str, Any]:
    """Read a flat JSON object of settings, rejecting unknown keys."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read config file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"config file {path} must contain a JSON object")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            raise ConfigurationError(f"unknown setting {key!r} in config file {path}")
        values[key] = tuple(value) if key == "plugins" else value
    return values


def load_settings(
    config_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    **overrides: Any,
) -> Settings:
    """Resolve settings from defaults, an optional config file, the environment and overrides.

    Args:
        config_path: JSON file holding a flat object of setting names to values.
        env: Environment mapping to read; defaults to :data:`os.environ`.
        **overrides: Explicit values that win over every other layer. ``None`` values
            are dropped, so callers can forward unset command-line options directly.

    Returns:
        A validated :class:`Settings` instance.

    Raises:
        ConfigurationError: A file is unreadable, a key is unknown, or a value has
            the wrong type.
    """
    environ = os.environ if env is None else env
    known = {f.name for f in fields(Settings)}
    values: dict[str, Any] = {}

    if config_path is not None:
        values.update(_load_config_file(Path(config_path), known))

    for name, declared in _TYPES.items():
        raw = environ.get(f"{ENV_PREFIX}{name.upper()}")
        if raw is not None:
            values[name] = _coerce(name, raw, declared)

    for key, value in overrides.items():
        if value is None:
            continue
        if key not in known:
            raise ConfigurationError(f"unknown setting {key!r}")
        values[key] = tuple(value) if key == "plugins" else value

    return Settings(**values)
