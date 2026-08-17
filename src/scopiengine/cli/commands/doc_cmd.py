"""``scopi doc`` — write and read individual documents — and ``scopi bulk``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from scopiengine.api.app import execute_bulk
from scopiengine.cli.output import echo, error, print_json
from scopiengine.engine import Engine

__all__ = ["app", "bulk"]

app = typer.Typer(name="doc", help="Write and read individual documents.", no_args_is_help=True)


@app.command("put")
def put(
    ctx: typer.Context,
    index: str,
    body: Annotated[str, typer.Argument(help="JSON document body, or '-' to read it from stdin.")],
    doc_id: Annotated[
        str | None, typer.Option("--id", help="External document id; auto-generated when omitted.")
    ] = None,
) -> None:
    """Index one document."""
    text = sys.stdin.read() if body == "-" else body
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"document body is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise typer.BadParameter("document body must be a JSON object")
    with Engine.open(ctx.obj.settings) as engine:
        ids = engine.index_documents(index, [doc], ids=[doc_id] if doc_id else None)
        # Each CLI invocation is its own short-lived process: unlike `scopi
        # serve`, where the writer's buffer survives across requests within
        # one long-running process, a document indexed here would otherwise
        # sit in an in-memory buffer that vanishes the moment this process
        # exits — durably stored (see IndexWriter's module docstring) but
        # never in a segment, so never actually searchable. Refreshing here
        # is what makes "put a doc, then search for it" work as two separate
        # `scopi` invocations, the only way the CLI can be used.
        engine.refresh(index)
    if ctx.obj.json_output:
        print_json({"_index": index, "_id": ids[0]})
    else:
        echo(f"indexed {ids[0]!r} into {index!r}")


@app.command("get")
def get(ctx: typer.Context, index: str, doc_id: str) -> None:
    """Fetch one document by id."""
    with Engine.open(ctx.obj.settings) as engine:
        doc = engine.get_document(index, doc_id)
    if doc is None:
        error(f"document {doc_id!r} not found in index {index!r}")
        raise typer.Exit(code=1)
    if ctx.obj.json_output:
        print_json({"_index": index, "_id": doc_id, "_source": doc})
    else:
        print_json(doc)


@app.command("delete")
def delete(ctx: typer.Context, index: str, doc_id: str) -> None:
    """Delete one document by id."""
    with Engine.open(ctx.obj.settings) as engine:
        found = engine.delete_document(index, doc_id)
    if not found:
        error(f"document {doc_id!r} not found in index {index!r}")
        raise typer.Exit(code=1)
    if ctx.obj.json_output:
        print_json({"_index": index, "_id": doc_id, "result": "deleted"})
    else:
        echo(f"deleted {doc_id!r} from {index!r}")


def bulk(
    ctx: typer.Context,
    index: str,
    file: Annotated[
        Path, typer.Option("--file", help="NDJSON file: an action/meta line then a source line.")
    ],
) -> None:
    """Bulk-index from an NDJSON file (ES bulk syntax: 'index'/'create'/'delete' actions)."""
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise typer.BadParameter(f"cannot read {file}: {exc}") from exc
    with Engine.open(ctx.obj.settings) as engine:
        result = execute_bulk(engine, index, raw)
        engine.refresh(index)  # see the comment in `put` above: this process is about to exit
    if ctx.obj.json_output:
        print_json(result)
    else:
        echo(f"bulk: {len(result['items'])} action(s), errors={result['errors']}")
        if result["errors"]:
            for entry in result["items"]:
                ((action, item),) = entry.items()
                if "error" in item or item.get("result") == "not_found":
                    echo(f"  {action} {item.get('_id')}: {item.get('error', item.get('result'))}")
    if result["errors"]:
        raise typer.Exit(code=1)
