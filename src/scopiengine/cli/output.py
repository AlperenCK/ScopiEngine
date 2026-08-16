"""Terminal output helpers shared by every CLI command.

Output goes to stdout, diagnostics to stderr, and every command accepts ``--json``
so the CLI composes with ``jq`` and friends. No rendering dependency: a table is
cheap enough to format by hand, and it keeps the install small.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = ["echo", "error", "print_json", "print_kv", "print_table", "truncate"]


def echo(message: str = "") -> None:
    """Write a line to stdout."""
    print(message, file=sys.stdout)


def error(message: str) -> None:
    """Write a line to stderr."""
    print(message, file=sys.stderr)


def print_json(data: Any) -> None:
    """Write ``data`` to stdout as indented JSON."""
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False), file=sys.stdout)


def truncate(value: Any, width: int) -> str:
    """Render ``value`` as a single line no wider than ``width`` characters."""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\t", " ")
    if width <= 0 or len(text) <= width:
        return text
    return text[: max(width - 1, 0)] + "…"


def print_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    max_width: int = 60,
) -> None:
    """Write a column-aligned table to stdout.

    Args:
        headers: Column titles.
        rows: Row values; each row must match the header count.
        max_width: Per-cell character budget before the value is elided.
    """
    materialised = [[truncate(cell, max_width) for cell in row] for row in rows]
    if not materialised:
        echo("  ".join(headers))
        echo("(no results)")
        return

    widths = [len(header) for header in headers]
    for row in materialised:
        for column, cell in enumerate(row):
            if column < len(widths):
                widths[column] = max(widths[column], len(cell))

    echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    echo("  ".join("-" * width for width in widths))
    for row in materialised:
        echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def print_kv(data: dict[str, Any], *, indent: int = 0) -> None:
    """Write aligned ``key: value`` lines, nesting one level for dict values."""
    if not data:
        return
    pad = " " * indent
    width = max(len(key) for key in data)
    for key, value in data.items():
        if isinstance(value, dict):
            echo(f"{pad}{key}:")
            print_kv(value, indent=indent + 2)
        else:
            rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else value
            echo(f"{pad}{key.ljust(width)} : {rendered}")
