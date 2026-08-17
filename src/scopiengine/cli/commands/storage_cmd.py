"""``scopi storage`` — initialise and inspect the storage backend."""

from __future__ import annotations

import typer

from scopiengine.cli.output import echo, print_json, print_kv
from scopiengine.storage.factory import open_storage

__all__ = ["app"]

app = typer.Typer(
    name="storage",
    help="Initialise and inspect the storage backend.",
    no_args_is_help=True,
)


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the storage schema if it does not exist yet."""
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        backend.migrate()
        payload = {"storage": settings.storage, "schema_version": backend.schema_version}
    if ctx.obj.json_output:
        print_json(payload)
    else:
        echo(f"initialised {settings.storage} (schema version {payload['schema_version']})")


@app.command()
def info(ctx: typer.Context) -> None:
    """Show the resolved storage DSN and whether the backend is reachable."""
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        payload = {
            "storage": settings.storage,
            "scheme": settings.storage.split("://", 1)[0],
            "schema_version": backend.schema_version,
            "reachable": backend.ping(),
        }
    if ctx.obj.json_output:
        print_json(payload)
    else:
        print_kv(payload)


@app.command()
def migrate(ctx: typer.Context) -> None:
    """Apply pending schema migrations, creating the schema on a fresh database."""
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        backend.migrate()
        payload = {"storage": settings.storage, "schema_version": backend.schema_version}
    if ctx.obj.json_output:
        print_json(payload)
    else:
        echo(f"migrated {settings.storage} to schema version {payload['schema_version']}")
