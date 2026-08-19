"""The web UI's login gate, end to end: with auth off (the default) the UI and
its account-management endpoints are wide open, exactly as before this
feature existed; with it on, an unauthenticated request is redirected (HTML)
or 401'd (``/_ui/api/*``), a Service Account can log in and out, and
disabling or deleting an account invalidates its session immediately rather
than waiting out the TTL.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopiengine.api import create_app
from scopiengine.settings import Settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Auth off — the default. Matches ``test_api.py``'s fixture of the same name."""
    settings = Settings(storage=f"sqlite:///{tmp_path / 'api.db'}")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(tmp_path: Path) -> Iterator[TestClient]:
    """Auth on, with no accounts yet — the state right after enabling it."""
    settings = Settings(
        storage=f"sqlite:///{tmp_path / 'api.db'}", ui_auth_enabled=True, ui_session_ttl=3600
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _create_account(client: TestClient, username: str, password: str) -> None:
    r = client.post("/_ui/api/accounts", json={"username": username, "password": password})
    assert r.status_code == 201, r.text


# -- auth off: unchanged behaviour, accounts still manageable -----------------


def test_auth_off_ui_and_session_endpoint_are_open(client: TestClient) -> None:
    assert client.get("/_ui/", follow_redirects=False).status_code == 200
    session = client.get("/_ui/api/session")
    assert session.status_code == 200
    assert session.json() == {"auth_enabled": False}


def test_auth_off_accounts_can_still_be_managed(client: TestClient) -> None:
    _create_account(client, "alice", "correct horse battery staple")
    listed = client.get("/_ui/api/accounts").json()
    assert [a["username"] for a in listed] == ["alice"]


# -- auth on: the gate ---------------------------------------------------------


def test_auth_on_ui_page_redirects_to_login_without_a_session(auth_client: TestClient) -> None:
    r = auth_client.get("/_ui/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/_ui/login.html?next=/_ui/"


def test_auth_on_api_endpoint_401s_without_a_session(auth_client: TestClient) -> None:
    r = auth_client.get("/_ui/api/accounts")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_auth_on_login_page_and_its_assets_stay_reachable(auth_client: TestClient) -> None:
    assert auth_client.get("/_ui/login.html").status_code == 200
    assert auth_client.get("/_ui/styles.css").status_code == 200
    assert auth_client.get("/_ui/img/logo.png").status_code == 200


def test_auth_on_account_creation_is_reachable_to_bootstrap_the_first_login(
    auth_client: TestClient,
) -> None:
    """`/_ui/api/accounts` (POST) is gated like everything else once auth is on
    — an operator bootstraps the very first account via `scopi ui-account
    create` instead, which talks to storage directly, not through this API.
    This test documents that the endpoint is indeed gated, not open as a
    special case.
    """
    r = auth_client.post(
        "/_ui/api/accounts", json={"username": "alice", "password": "irrelevant123"}
    )
    assert r.status_code == 401


# -- login / logout -------------------------------------------------------------


def test_login_with_correct_credentials_sets_a_working_session(auth_client: TestClient) -> None:
    # Bootstrap the account the way `scopi ui-account create` would: directly
    # through the engine the app already opened, bypassing the (gated) API.
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")

    login = auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )
    assert login.status_code == 200
    assert login.json() == {"principal": "alice", "auth_method": "service_account"}
    assert "scopi_ui_session" in login.cookies

    session = auth_client.get("/_ui/api/session")
    assert session.status_code == 200
    assert session.json() == {
        "auth_enabled": True,
        "principal": "alice",
        "auth_method": "service_account",
    }

    ui_page = auth_client.get("/_ui/", follow_redirects=False)
    assert ui_page.status_code == 200


def test_session_cookie_is_scoped_to_the_whole_site_not_just_ui(
    auth_client: TestClient,
) -> None:
    """`Path=/`, not `/_ui/` — behind a reverse proxy that maps a public
    subpath onto this app's root (see docs/UI_AUTH.md), the browser's real
    request path is `/<subpath>/_ui/...`, which a `/_ui/`-scoped cookie would
    never match and so would silently never be sent back.
    """
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    login = auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )
    assert "Path=/;" in login.headers["set-cookie"]
    assert "Path=/_ui/" not in login.headers["set-cookie"]


def test_login_with_wrong_password_is_rejected(auth_client: TestClient) -> None:
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    login = auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "wrong password"}
    )
    assert login.status_code == 401
    assert "scopi_ui_session" not in login.cookies


def test_login_with_unknown_username_is_rejected(auth_client: TestClient) -> None:
    login = auth_client.post(
        "/_ui/api/login", json={"username": "nobody", "password": "irrelevant"}
    )
    assert login.status_code == 401


def test_logout_invalidates_the_session(auth_client: TestClient) -> None:
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )
    assert auth_client.get("/_ui/api/session").status_code == 200

    logout = auth_client.post("/_ui/api/logout")
    assert logout.status_code == 200
    assert auth_client.get("/_ui/api/session").status_code == 401


# -- disabling/deleting an account revokes its session immediately ------------


def test_disabling_the_account_invalidates_its_session_on_the_next_request(
    auth_client: TestClient,
) -> None:
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )
    assert auth_client.get("/_ui/api/session").status_code == 200

    engine = auth_client.app.state.engine  # type: ignore[attr-defined]
    engine.storage.set_ui_account_disabled("alice", True)

    assert auth_client.get("/_ui/api/session").status_code == 401


def test_deleting_the_account_invalidates_its_session_on_the_next_request(
    auth_client: TestClient,
) -> None:
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )
    assert auth_client.get("/_ui/api/session").status_code == 200

    engine = auth_client.app.state.engine  # type: ignore[attr-defined]
    engine.storage.delete_ui_account("alice")

    assert auth_client.get("/_ui/api/session").status_code == 401


# -- account management once logged in ------------------------------------------


def test_logged_in_account_can_manage_other_accounts(auth_client: TestClient) -> None:
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )

    created = auth_client.post(
        "/_ui/api/accounts", json={"username": "bob", "password": "another good password"}
    )
    assert created.status_code == 201

    listed = auth_client.get("/_ui/api/accounts").json()
    assert {a["username"] for a in listed} == {"alice", "bob"}

    disabled = auth_client.post("/_ui/api/accounts/bob/disable")
    assert disabled.status_code == 200
    assert disabled.json() == {"username": "bob", "disabled": True}

    deleted = auth_client.delete("/_ui/api/accounts/bob")
    assert deleted.status_code == 200
    assert deleted.json() == {"username": "bob", "deleted": True}
    assert auth_client.delete("/_ui/api/accounts/bob").status_code == 404


def test_create_account_validates_username_and_password(auth_client: TestClient) -> None:
    _bootstrap_account(auth_client, "alice", "correct horse battery staple")
    auth_client.post(
        "/_ui/api/login", json={"username": "alice", "password": "correct horse battery staple"}
    )

    missing_username = auth_client.post("/_ui/api/accounts", json={"password": "goodpassword1"})
    assert missing_username.status_code == 400

    short_password = auth_client.post(
        "/_ui/api/accounts", json={"username": "bob", "password": "short"}
    )
    assert short_password.status_code == 400


def _bootstrap_account(client: TestClient, username: str, password: str) -> None:
    """Create an account the way ``scopi ui-account create`` would — directly
    against storage, bypassing the (gated) API — so login tests do not
    depend on the account-creation endpoint working.
    """
    from datetime import UTC, datetime

    from scopiengine.auth.passwords import hash_password
    from scopiengine.storage.models import UIAccount

    engine = client.app.state.engine  # type: ignore[attr-defined]
    engine.storage.create_ui_account(
        UIAccount(
            username=username,
            password_hash=hash_password(password),
            disabled=False,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
