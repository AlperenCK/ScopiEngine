"""``scopi index`` — create, list, inspect, refresh, merge and delete indices."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from scopiengine.cli.output import echo, print_json, print_kv, print_table
from scopiengine.engine import Engine

__all__ = ["app"]

app = typer.Typer(
    name="index",
    help="Create, list, inspect, refresh, merge and delete indices.",
    no_args_is_help=True,
)


def _load_json_file(path: Path | None, label: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(f"cannot read {label} file {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{label} file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{label} file {path} must contain a JSON object")
    return data


@app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    mapping: Annotated[
        Path | None, typer.Option("--mapping", help="JSON mapping file: {'properties': {...}}.")
    ] = None,
    index_settings: Annotated[
        Path | None, typer.Option("--settings", help="JSON index settings file.")
    ] = None,
) -> None:
    """Create an index. Dynamic mapping applies to any field the mapping omits."""
    mapping_body = _load_json_file(mapping, "mapping")
    settings_body = _load_json_file(index_settings, "settings")
    with Engine.open(ctx.obj.settings) as engine:
        info = engine.create_index(name, mapping_body, settings_body)
    payload = {"name": info.name, "doc_count": info.doc_count, "created_at": info.created_at}
    if ctx.obj.json_output:
        print_json(payload)
    else:
        echo(f"created index {info.name!r}")


@app.command("list")
def list_indices(ctx: typer.Context) -> None:
    """List every index."""
    with Engine.open(ctx.obj.settings) as engine:
        indices = engine.list_indices()
    if ctx.obj.json_output:
        print_json(
            [
                {"name": i.name, "doc_count": i.doc_count, "created_at": i.created_at}
                for i in indices
            ]
        )
        return
    print_table(
        ["name", "doc_count", "created_at"],
        [(i.name, i.doc_count, i.created_at) for i in indices],
    )


@app.command("show")
def show(ctx: typer.Context, name: str) -> None:
    """Show one index's mapping, settings and document count."""
    with Engine.open(ctx.obj.settings) as engine:
        info = engine.get_index(name)
    payload = {
        "name": info.name,
        "mapping": info.mapping,
        "settings": info.settings,
        "doc_count": info.doc_count,
        "created_at": info.created_at,
    }
    if ctx.obj.json_output:
        print_json(payload)
    else:
        print_kv(payload)


@app.command("delete")
def delete(ctx: typer.Context, name: str) -> None:
    """Delete an index and everything stored beneath it."""
    with Engine.open(ctx.obj.settings) as engine:
        engine.delete_index(name)
    if ctx.obj.json_output:
        print_json({"acknowledged": True, "name": name})
    else:
        echo(f"deleted index {name!r}")


@app.command("stats")
def stats(ctx: typer.Context, name: str) -> None:
    """Show an index's document, segment and buffer statistics."""
    with Engine.open(ctx.obj.settings) as engine:
        payload = engine.stats(name)
    if ctx.obj.json_output:
        print_json(payload)
    else:
        print_kv(payload)


@app.command("refresh")
def refresh(ctx: typer.Context, name: str) -> None:
    """Flush the pending write buffer to a new, searchable segment."""
    with Engine.open(ctx.obj.settings) as engine:
        segment_id = engine.refresh(name)
    payload = {"name": name, "flushed_segment": segment_id}
    if ctx.obj.json_output:
        print_json(payload)
    elif segment_id is None:
        echo(f"refreshed {name!r} (nothing buffered)")
    else:
        echo(f"refreshed {name!r} (segment {segment_id})")


@app.command("merge")
def merge(ctx: typer.Context, name: str) -> None:
    """Force-merge every live segment into one."""
    with Engine.open(ctx.obj.settings) as engine:
        segment_id = engine.force_merge(name)
    payload = {"name": name, "merged_segment": segment_id}
    if ctx.obj.json_output:
        print_json(payload)
    elif segment_id is None:
        echo(f"merged {name!r} (nothing to merge)")
    else:
        echo(f"merged {name!r} into segment {segment_id}")
