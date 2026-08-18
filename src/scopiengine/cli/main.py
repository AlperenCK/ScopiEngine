"""``scopi`` command-line entry point.

Global options are resolved once in :func:`main` and stashed on the Typer context,
so every subcommand reads the same :class:`~scopiengine.settings.Settings`.
Subcommand groups register themselves in :func:`_register_commands`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from scopiengine import TAGLINE, __version__
from scopiengine.cli.output import echo, error, print_json, print_kv
from scopiengine.errors import ScopiError
from scopiengine.logging_conf import configure_logging
from scopiengine.settings import Settings, load_settings

__all__ = ["CliContext", "app", "main", "run"]

#: Exit code used when a ScopiEngine error reaches the top level.
EXIT_ERROR = 1
#: Exit code used when the user interrupts a command.
EXIT_INTERRUPTED = 130


@dataclass(slots=True)
class CliContext:
    """State shared by every subcommand, attached to ``ctx.obj``."""

    settings: Settings
    json_output: bool


app = typer.Typer(
    name="scopi",
    help=f"ScopiEngine — {TAGLINE}",
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    """Print the version and exit, before any other option is processed."""
    if value:
        echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    storage: Annotated[
        str | None,
        typer.Option("--storage", "-s", envvar="SCOPI_STORAGE", help="Storage DSN."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", envvar="SCOPI_CONFIG", help="JSON config file."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of tables."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log at DEBUG level."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Log errors only."),
    ] = False,
    strict_plugins: Annotated[
        bool,
        typer.Option("--strict-plugins", help="Fail startup when a plugin raises."),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """Resolve global options and store them for the subcommands."""
    log_level = "DEBUG" if verbose else "ERROR" if quiet else None
    settings = load_settings(
        config_path=config,
        storage=storage,
        log_level=log_level,
        strict_plugins=strict_plugins or None,
    )
    configure_logging(settings.log_level, settings.log_format)
    ctx.obj = CliContext(settings=settings, json_output=json_output)


@app.command()
def version(ctx: typer.Context) -> None:
    """Print the ScopiEngine version."""
    if ctx.obj is not None and ctx.obj.json_output:
        print_json({"name": "scopiengine", "version": __version__})
    else:
        echo(__version__)


@app.command()
def info(ctx: typer.Context) -> None:
    """Show the version and the resolved configuration."""
    payload = {
        "name": "scopiengine",
        "version": __version__,
        "tagline": TAGLINE,
        "settings": ctx.obj.settings.to_dict(),
    }
    if ctx.obj.json_output:
        print_json(payload)
        return
    echo(f"ScopiEngine {__version__} — {TAGLINE}")
    echo("")
    print_kv(ctx.obj.settings.to_dict())


def _register_commands() -> None:
    """Attach optional command groups, skipping any not present in this build."""
    from scopiengine.cli.commands.doc_cmd import app as doc_app
    from scopiengine.cli.commands.doc_cmd import bulk
    from scopiengine.cli.commands.index_cmd import app as index_app
    from scopiengine.cli.commands.ingest_cmd import app as ingest_app
    from scopiengine.cli.commands.plugin_cmd import app as plugin_app
    from scopiengine.cli.commands.search_cmd import analyze, search
    from scopiengine.cli.commands.serve_cmd import serve
    from scopiengine.cli.commands.storage_cmd import app as storage_app
    from scopiengine.cli.commands.ui_account_cmd import app as ui_account_app

    app.add_typer(storage_app, name="storage")
    app.add_typer(index_app, name="index")
    app.add_typer(doc_app, name="doc")
    app.add_typer(ingest_app, name="ingest")
    app.add_typer(plugin_app, name="plugin")
    app.add_typer(ui_account_app, name="ui-account")
    app.command("bulk", help="Bulk-index documents from an NDJSON file.")(bulk)
    app.command("search", help="Search an index with ScopiQL or a JSON DSL body.")(search)
    app.command("analyze", help="Run a named analyzer over text.")(analyze)
    app.command("serve", help="Run the REST API.")(serve)


_register_commands()


def run() -> None:
    """Console-script entry point with top-level error handling."""
    try:
        app()
    except ScopiError as exc:
        error(f"error [{exc.error_type}]: {exc.reason}")
        sys.exit(EXIT_ERROR)
    except KeyboardInterrupt:
        error("interrupted")
        sys.exit(EXIT_INTERRUPTED)


if __name__ == "__main__":
    run()
