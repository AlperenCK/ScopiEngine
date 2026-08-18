"""ASGI middleware gating the bundled web UI behind a login.

Only active when :attr:`~scopiengine.settings.Settings.ui_auth_enabled` is
set — the REST API itself is never touched by this middleware, and neither
is the UI when the setting is off (the default).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from scopiengine.auth.sessions import SESSION_COOKIE_NAME, resolve_session

__all__ = ["UIAuthMiddleware"]

#: Paths under ``/_ui/`` reachable without a session — the login page, the
#: assets it needs to render itself, and the endpoint that establishes one.
_PUBLIC_UI_PATHS = frozenset(
    {
        "/_ui/login.html",
        "/_ui/styles.css",
        "/_ui/img/logo.png",
        "/_ui/api/login",
    }
)


class UIAuthMiddleware(BaseHTTPMiddleware):
    """Redirect (HTML) or 401 (``/_ui/api/*``) any ``/_ui/*`` request lacking
    a valid session, once ``ui_auth_enabled`` is on.

    Runs for every request, not only ``/_ui/*`` ones, but returns immediately
    for anything outside that prefix — the REST API's request path is
    unaffected either way.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not path.startswith("/_ui/") or path in _PUBLIC_UI_PATHS:
            return await call_next(request)

        engine = request.app.state.engine
        if not engine.settings.ui_auth_enabled:
            return await call_next(request)

        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        session = resolve_session(engine.storage, raw_token) if raw_token else None
        if session is not None and session.auth_method == "service_account":
            # A session outlives the account it was issued for by default —
            # disabling or deleting the account should take effect on the
            # next request, not wait out the session's remaining TTL (up to
            # `ui_session_ttl`, 12h by default).
            account = engine.storage.get_ui_account(session.principal)
            if account is None or account.disabled:
                engine.storage.delete_ui_session(session.session_id_hash)
                session = None
        if session is None:
            if path.startswith("/_ui/api/"):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {"type": "authentication_error", "reason": "login required"},
                        "status": 401,
                    },
                )
            next_path = path
            if request.url.query:
                next_path += f"?{request.url.query}"
            return RedirectResponse(url=f"/_ui/login.html?next={next_path}")

        request.state.ui_principal = session.principal
        request.state.ui_auth_method = session.auth_method
        return await call_next(request)
