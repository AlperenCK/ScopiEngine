"""``scopi serve`` — run the REST API."""

from __future__ import annotations

from typing import Annotated

import typer

from scopiengine.cli.output import echo

__all__ = ["serve"]


def serve(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", help="Interface to bind.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="TCP port to bind.")] = None,
) -> None:
    """Run the REST API with uvicorn, sharing the resolved settings and storage DSN."""
    import uvicorn

    from scopiengine.api import create_app

    settings = ctx.obj.settings
    bind_host = host or settings.host
    bind_port = port or settings.port
    app = create_app(settings)
    echo(f"scopi serve: listening on {bind_host}:{bind_port} (storage={settings.storage})")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level=settings.log_level.lower())
