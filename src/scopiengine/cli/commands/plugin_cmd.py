"""``scopi plugin`` — inspect discovered plugins."""

from __future__ import annotations

import typer

from scopiengine.cli.output import print_json, print_table
from scopiengine.plugins.registry import discover_plugins

__all__ = ["app"]

app = typer.Typer(name="plugin", help="Inspect discovered plugins.", no_args_is_help=True)


@app.command("list")
def list_plugins(ctx: typer.Context) -> None:
    """List every plugin discovered at startup, and whether it loaded."""
    registry = discover_plugins(ctx.obj.settings)
    rows: list[tuple[str, str, str]] = [(name, "ok", "") for name in registry.loaded]
    rows += [(r.name, "failed", r.error or "") for r in registry.failed]
    if ctx.obj.json_output:
        print_json(
            {
                "loaded": list(registry.loaded),
                "failed": [{"name": r.name, "error": r.error} for r in registry.failed],
            }
        )
        return
    print_table(["name", "status", "error"], rows)
