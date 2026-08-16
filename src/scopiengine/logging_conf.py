"""Logging setup for the CLI, the server and the ingestion pipeline.

Two formats are supported: ``text`` for a human at a terminal, and ``json`` for
shipping the engine's own logs into a collector — including, fittingly, ScopiEngine.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

__all__ = ["JsonFormatter", "configure_logging", "get_logger"]

#: Root logger name; every module logger hangs beneath it.
ROOT_LOGGER = "scopiengine"

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

#: Attributes present on every LogRecord, excluded when collecting extra fields.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render records as one JSON object per line, preserving ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "@timestamp": self.formatTime(record, _TIME_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "text") -> logging.Logger:
    """Configure the ScopiEngine logger and return it.

    Logs go to stderr so that stdout stays reserved for command output — which keeps
    ``scopi search --json ... | jq`` usable while logging is on.

    Args:
        level: Level name such as ``DEBUG`` or ``INFO``. Unknown names fall back to ``INFO``.
        fmt: ``text`` or ``json``.

    Returns:
        The configured root ScopiEngine logger.
    """
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT, _TIME_FORMAT)
    )
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger beneath the ScopiEngine root.

    Args:
        name: Usually ``__name__``. The ``scopiengine.`` prefix is added when absent.
    """
    if not name or name == ROOT_LOGGER:
        return logging.getLogger(ROOT_LOGGER)
    if name.startswith(f"{ROOT_LOGGER}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")
