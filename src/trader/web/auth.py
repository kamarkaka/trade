"""Auth middleware + session-guard dependency for the monitoring UI (design §19.5, M7.4).

``require_session`` is the SINGLE auth chokepoint applied to every monitoring router: it
validates the signed session cookie (via the M7.3 primitives, injected clock from
``app.state.now``) and, on success, schedules an idle-sliding cookie refresh; on failure it
raises a redirect to ``/login`` (303) for browsers or 401 for HTMX/JSON callers.

Cookie flags: HttpOnly, SameSite=strict, Path=/, no Domain, and Secure (TLS-only) unless
``settings.cookie_secure`` is disabled for http test clients. The idle refresh is applied by
an outgoing middleware (``install_session_refresh``) so it works no matter how a route builds
its response (the FastAPI ``Response``-param merge does not cover explicit Response returns).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from trader.web.security import read_session_token, refresh_session_token
from trader.web.settings import WebSettings

SESSION_COOKIE = "session"
_REFRESH_ATTR = "refreshed_session"


def _secret(settings: WebSettings) -> str:
    return settings.session_secret.get_secret_value()


def _wants_redirect(request: Request) -> bool:
    # HTMX / fetch callers get a 401 they can handle; full-page browser nav gets a 303.
    if request.headers.get("HX-Request") == "true":
        return False
    return "text/html" in request.headers.get("accept", "")


def _needs_auth(request: Request) -> HTTPException:
    if _wants_redirect(request):
        return HTTPException(status_code=303, headers={"Location": "/login"})
    return HTTPException(status_code=401, detail="authentication required")


def require_session(request: Request) -> str:
    """FastAPI dependency: return the authenticated username or raise (redirect/401).

    On success, stashes a refreshed (idle-slid, absolute-preserving) token on
    ``request.state`` for the refresh middleware to set on the response."""
    settings: WebSettings = request.app.state.settings
    now = request.app.state.now()
    token = request.cookies.get(SESSION_COOKIE)
    user = (
        read_session_token(
            _secret(settings),
            token,
            now,
            idle_seconds=settings.session_idle_seconds,
            absolute_seconds=settings.session_absolute_seconds,
        )
        if token
        else None
    )
    if user is None or token is None:
        raise _needs_auth(request)
    refreshed = refresh_session_token(
        _secret(settings),
        token,
        now,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    if refreshed is not None:
        setattr(request.state, _REFRESH_ATTR, refreshed)
    return user


def set_session_cookie(response: Response, token: str, settings: WebSettings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_absolute_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response, settings: WebSettings) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def install_session_refresh(app: FastAPI) -> None:
    """Register the middleware that writes the idle-refreshed session cookie onto the
    outgoing response when ``require_session`` validated the request."""

    @app.middleware("http")
    async def _refresh(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        refreshed = getattr(request.state, _REFRESH_ATTR, None)
        if refreshed is not None:
            set_session_cookie(response, refreshed, request.app.state.settings)
        return response


__all__ = [
    "SESSION_COOKIE",
    "clear_session_cookie",
    "install_session_refresh",
    "require_session",
    "set_session_cookie",
]
