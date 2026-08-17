"""``scopi search`` / ``scopi analyze`` — query and analyze without a server running.

Both drive :class:`~scopiengine.engine.Engine` directly, through the exact same
:mod:`scopiengine.query.results` entry points ``scopi serve``'s ``_search`` and
``_analyze`` endpoints call — so a query gives the same answer whether or not
a server happens to be running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from scopiengine.cli.output import echo, print_json
from scopiengine.engine import Engine
from scopiengine.query.results import run_dsl, run_scopiql

__all__ = ["analyze", "search"]


def search(
    ctx: typer.Context,
    index: str,
    query: Annotated[
        str | None, typer.Argument(help="ScopiQL query string (omit when using --dsl).")
    ] = None,
    dsl_file: Annotated[
        Path | None, typer.Option("--dsl", help="JSON DSL request body file, instead of ScopiQL.")
    ] = None,
    size: Annotated[int, typer.Option("--size", help="Maximum hits to return.")] = 10,
    from_: Annotated[int, typer.Option("--from", help="Number of top hits to skip.")] = 0,
    explain: Annotated[
        bool, typer.Option("--explain", help="Also print the compiled query AST and timings.")
    ] = False,
) -> None:
    """Search an index with a ScopiQL query, or a JSON DSL body via --dsl."""
    if dsl_file is not None and query:
        raise typer.BadParameter("pass either a ScopiQL query or --dsl, not both")
    with Engine.open(ctx.obj.settings) as engine:
        if dsl_file is not None:
            try:
                body: dict[str, Any] = json.loads(dsl_file.read_text(encoding="utf-8"))
            except OSError as exc:
                raise typer.BadParameter(f"cannot read {dsl_file}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise typer.BadParameter(f"{dsl_file} is not valid JSON: {exc}") from exc
            body.setdefault("size", size)
            body.setdefault("from", from_)
            envelope = run_dsl(engine, index, body)
        else:
            envelope = run_scopiql(engine, index, query or "", size=size, from_=from_)

    if ctx.obj.json_output:
        print_json(envelope)
        return

    hits = envelope["hits"]
    echo(f"{hits['total']['value']} hit(s), took {envelope['took']}ms")
    for hit in hits["hits"]:
        source = json.dumps(hit.get("_source", {}), ensure_ascii=False)
        echo(f"  {hit['_id']}  score={hit['_score']:.4f}  {source}")
    if "aggregations" in envelope:
        echo("")
        echo("aggregations:")
        echo(json.dumps(envelope["aggregations"], indent=2, ensure_ascii=False))
    if explain:
        echo("")
        echo("scopi:")
        echo(json.dumps(envelope["scopi"], indent=2, ensure_ascii=False))


def analyze(
    ctx: typer.Context,
    text: str,
    analyzer: Annotated[
        str, typer.Option("--analyzer", help="Named analyzer to run.")
    ] = "standard",
) -> None:
    """Run a named analyzer over text and print the resulting (term, position) tokens."""
    with Engine.open(ctx.obj.settings) as engine:
        tokens = engine.analyze(text, analyzer=analyzer)
    payload = [{"token": term, "position": position} for term, position in tokens]
    if ctx.obj.json_output:
        print_json({"tokens": payload})
        return
    for entry in payload:
        echo(f"{entry['position']:>3}  {entry['token']}")
