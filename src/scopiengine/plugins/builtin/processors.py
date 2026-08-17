"""Built-in ``ingest_processor`` implementations: ``json_line``, ``regex_extract``, ``timestamp``.

Registered through the exact same ``ingest_processor`` hook
(:mod:`scopiengine.plugins.hooks`) a third-party package would use — see
:func:`register_ingest_processors` — so nothing about these three is privileged.

Every processor here follows the same contract:
``process(doc: dict, ctx: IngestContext) -> dict | None``. ``doc`` always starts
as ``{"message": <record text>}`` (the pipeline's seed shape — see
:mod:`scopiengine.ingest.pipeline`) and is threaded through the configured
processor chain in order, each one free to add, replace or rewrite fields.
None of the three ever returns ``None`` (they never drop a record) — dropping
is a capability the contract supports for a custom processor, not something
these reference implementations need.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from scopiengine.plugins.spec import IngestContext, IngestProcessor

__all__ = [
    "json_line",
    "regex_extract",
    "register_ingest_processors",
    "timestamp",
]

# -- json_line -------------------------------------------------------------


def json_line(doc: dict[str, Any], _ctx: IngestContext) -> dict[str, Any]:
    """Parse ``doc["message"]`` as a JSON object, replacing ``doc`` with its fields.

    A line that is not valid JSON, or that parses to something other than a
    JSON object (an array, a bare string/number), is left exactly as it
    arrived — the raw ``message`` field is the fallback, never a dropped record.
    """
    message = doc.get("message")
    if not isinstance(message, str):
        return doc
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return doc
    if not isinstance(parsed, dict):
        return doc
    return parsed


# -- regex_extract -----------------------------------------------------------

#: The ``IngestContext`` key :func:`regex_extract` reads its pattern from — a
#: ``str`` or a pre-compiled ``re.Pattern``, with named groups
#: (``(?P<name>...)``) becoming document fields. Populated by the CLI's
#: ``--regex-pattern`` option, or set directly by a caller embedding the pipeline.
REGEX_PATTERN_CTX_KEY = "regex_pattern"


def regex_extract(doc: dict[str, Any], ctx: IngestContext) -> dict[str, Any]:
    """Match ``doc["message"]`` against ``ctx["regex_pattern"]``, adding named groups as fields.

    A no-op (returns ``doc`` unchanged) when no pattern is configured, the
    message field is missing or non-string, or the pattern does not match —
    never a dropped record.
    """
    pattern = ctx.get(REGEX_PATTERN_CTX_KEY)
    if not pattern:
        return doc
    compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
    message = doc.get("message")
    if not isinstance(message, str):
        return doc
    match = compiled.search(message)
    if match is None:
        return doc
    extracted = {name: value for name, value in match.groupdict().items() if value is not None}
    if not extracted:
        return doc
    return {**doc, **extracted}


# -- timestamp ---------------------------------------------------------------

#: ``IngestContext`` key overriding which fields :func:`timestamp` checks, and
#: in what order, before falling back to a leading timestamp in ``message``.
TIMESTAMP_FIELDS_CTX_KEY = "timestamp_fields"

_DEFAULT_TIMESTAMP_FIELDS: tuple[str, ...] = ("@timestamp", "timestamp", "time", "ts", "date")

#: ``(format, assume_current_year)`` pairs tried, in order, after ISO-8601 and
#: RFC 2822 both fail. ``assume_current_year`` is for formats (syslog's
#: classic ``Mon DD HH:MM:SS``) that carry no year of their own.
_STRPTIME_FORMATS: tuple[tuple[str, bool], ...] = (
    ("%d/%b/%Y:%H:%M:%S %z", False),  # Apache/nginx combined log format
    ("%Y-%m-%d %H:%M:%S,%f", False),  # log4j-style, comma-separated millis
    ("%Y-%m-%d %H:%M:%S", False),
    ("%Y/%m/%d %H:%M:%S", False),
    ("%b %d %H:%M:%S", True),  # classic syslog: "Jan 15 10:30:00"
)


def timestamp(doc: dict[str, Any], ctx: IngestContext) -> dict[str, Any]:
    """Parse a timestamp from a known field (or the start of ``message``) into ``@timestamp``.

    Tries, in order: ``ctx["timestamp_fields"]`` (default ``@timestamp`` /
    ``timestamp`` / ``time`` / ``ts`` / ``date``), then a timestamp-shaped
    prefix of ``message``. Recognises epoch seconds/milliseconds, ISO-8601,
    RFC 2822, Apache/nginx combined log format and classic syslog. A field
    that does not parse under any of these is skipped, not raised on; when
    nothing parses, ``doc`` is returned unchanged (no ``@timestamp`` added).
    """
    field_names = ctx.get(TIMESTAMP_FIELDS_CTX_KEY, _DEFAULT_TIMESTAMP_FIELDS)
    for name in field_names:
        value = doc.get(name)
        if value is None:
            continue
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return {**doc, "@timestamp": parsed}
    message = doc.get("message")
    if isinstance(message, str):
        parsed = _parse_leading_timestamp(message)
        if parsed is not None:
            return {**doc, "@timestamp": parsed}
    return doc


#: Cap on how many leading words of a raw message are tried as a timestamp
#: prefix. Kept small and searched narrowest-first (see
#: :func:`_parse_leading_timestamp`) on purpose: this runs once per ingested
#: record, so a wide, worst-case-first search here is the difference between
#: ingest throughput that holds up at real log-file scale and one that does
#: not - the common cases (an ISO-8601 or epoch token glued to the front)
#: resolve at width 1, on the very first attempt.
_MAX_LEADING_TIMESTAMP_WORDS = 4


def _parse_leading_timestamp(message: str) -> str | None:
    """Try progressively longer leading prefixes of a line as a timestamp.

    Handles the common case of a fixed-width timestamp glued to the front of
    a raw log line (syslog, CLF) with no delimiter marking where it ends.
    Narrowest-first: single-token formats (ISO-8601, epoch), by far the
    common case, then resolve on the first, cheapest attempt.
    """
    words = message.split(maxsplit=_MAX_LEADING_TIMESTAMP_WORDS)
    limit = min(_MAX_LEADING_TIMESTAMP_WORDS, len(words))
    for width in range(1, limit + 1):
        candidate = " ".join(words[:width]).rstrip(":,;")
        parsed = _parse_timestamp(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_timestamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _from_epoch(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # Cheapest and most common first: a bare epoch number, then ISO-8601 (a
    # fast C-level parse). Only a value that fails both pays for the
    # heavier, log-format-specific attempts below.
    if _looks_like_epoch(text):
        try:
            return _from_epoch(float(text))
        except ValueError:
            pass

    iso_candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        return _normalize(datetime.fromisoformat(iso_candidate))
    except ValueError:
        pass

    for fmt, assume_current_year in _STRPTIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if assume_current_year:
            parsed = parsed.replace(year=datetime.now(UTC).year)
        return _normalize(parsed)

    # RFC 2822 last: the least likely shape in a log line, and the most
    # permissive/expensive parser of the bunch.
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return _normalize(parsed) if parsed is not None else None


def _looks_like_epoch(text: str) -> bool:
    core = text.split(".", 1)[0]
    return core.lstrip("-").isdigit() and 9 <= len(core.lstrip("-")) <= 14


def _from_epoch(value: float) -> str | None:
    seconds = value / 1000.0 if abs(value) >= 1_000_000_000_000 else value
    try:
        return _normalize(datetime.fromtimestamp(seconds, tz=UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _normalize(dt: datetime) -> str:
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- registration --------------------------------------------------------------


def register_ingest_processors() -> dict[str, IngestProcessor]:
    """Register ``json_line``, ``regex_extract`` and ``timestamp``."""
    return {"json_line": json_line, "regex_extract": regex_extract, "timestamp": timestamp}
