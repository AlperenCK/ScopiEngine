"""``scopi ui-account`` — manage local web UI Service Accounts.

Exists mostly to bootstrap the very first account: once
``SCOPI_UI_AUTH_ENABLED`` is on, logging into ``/_ui/`` requires one already
existing, and the UI itself has no way to create it without being logged in
first — this is the way in.
"""

from __future__ import annotations

from datetime import UTC, datetime

import typer

from scopiengine.auth.passwords import hash_password
from scopiengine.cli.output import echo, print_json, print_table
from scopiengine.errors import ConfigurationError
from scopiengine.storage.factory import open_storage
from scopiengine.storage.models import UIAccount

__all__ = ["app"]

app = typer.Typer(
    name="ui-account",
    help="Manage local web UI Service Accounts.",
    no_args_is_help=True,
)


@app.command("create")
def create(
    ctx: typer.Context,
    username: str = typer.Argument(..., help="Login name, unique among UI accounts."),
) -> None:
    """Create a Service Account, prompting for its password."""
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        raise ConfigurationError("password must be at least 8 characters")
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        account = UIAccount(
            username=username,
            password_hash=hash_password(password),
            disabled=False,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        backend.create_ui_account(account)
    if ctx.obj.json_output:
        print_json({"username": username, "disabled": False})
    else:
        echo(f"created UI account {username!r}")


@app.command("list")
def list_accounts(ctx: typer.Context) -> None:
    """List every UI account."""
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        accounts = backend.list_ui_accounts()
    if ctx.obj.json_output:
        print_json(
            [
                {"username": a.username, "disabled": a.disabled, "created_at": a.created_at}
                for a in accounts
            ]
        )
        return
    rows = [(a.username, "disabled" if a.disabled else "enabled", a.created_at) for a in accounts]
    print_table(["username", "status", "created_at"], rows)


def _set_disabled(ctx: typer.Context, username: str, disabled: bool) -> None:
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        found = backend.set_ui_account_disabled(username, disabled)
    if not found:
        raise ConfigurationError(f"no such UI account: {username!r}")
    state = "disabled" if disabled else "enabled"
    if ctx.obj.json_output:
        print_json({"username": username, "disabled": disabled})
    else:
        echo(f"{username!r} is now {state}")


@app.command("disable")
def disable(
    ctx: typer.Context, username: str = typer.Argument(..., help="Account to disable.")
) -> None:
    """Disable a UI account without deleting it."""
    _set_disabled(ctx, username, True)


@app.command("enable")
def enable(
    ctx: typer.Context, username: str = typer.Argument(..., help="Account to re-enable.")
) -> None:
    """Re-enable a previously disabled UI account."""
    _set_disabled(ctx, username, False)


@app.command("delete")
def delete(
    ctx: typer.Context, username: str = typer.Argument(..., help="Account to delete.")
) -> None:
    """Delete a UI account. Any session it currently holds is invalidated on
    its next request, not just once the session's TTL naturally expires.
    """
    settings = ctx.obj.settings
    with open_storage(settings.storage) as backend:
        found = backend.delete_ui_account(username)
    if not found:
        raise ConfigurationError(f"no such UI account: {username!r}")
    if ctx.obj.json_output:
        print_json({"username": username, "deleted": True})
    else:
        echo(f"deleted UI account {username!r}")
