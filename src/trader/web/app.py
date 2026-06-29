"""FastAPI app factory for the read-only monitoring UI (design §19, M7.2).

``create_app(settings)`` builds the app with: the read-only state DB wired into app state,
Jinja2 templates + a /static mount, a PUBLIC ``/healthz`` (used by the compose healthcheck),
and a global exception handler that turns ANY unhandled route error into a generic 500 page
(logged to the web service's own ``trader.web`` logger) instead of crashing uvicorn. That
request-level isolation, plus running in a SEPARATE container, is how a UI fault never
touches the trader (the cross-container guarantee is checked in M7.11).

Isolation: this module imports only fastapi/starlette/jinja2/stdlib + ``trader.web.*`` — never
``trader.broker/schwab/execution/auth`` (guard test in M7.10).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trader.web.auth import install_session_refresh, require_session
from trader.web.db import ReadOnlyStateDB
from trader.web.routes import auth_routes
from trader.web.security import LoginThrottle
from trader.web.settings import WebSettings

_WEB_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _WEB_DIR / "static"
_TEMPLATES_DIR = _WEB_DIR / "templates"

logger = logging.getLogger("trader.web")  # web's OWN logger (stdout) — never the trading DB


def create_app(settings: WebSettings, *, now: Callable[[], datetime] | None = None) -> FastAPI:
    """Build the read-only monitoring FastAPI app from injected settings.

    ``now`` injects the clock (default: real UTC wall clock) so session/lockout timing is
    deterministic in tests."""
    app = FastAPI(title="trader monitor", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.now = now if now is not None else (lambda: datetime.now(UTC))
    app.state.db = ReadOnlyStateDB(settings.db_path)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.login_throttle = LoginThrottle(
        settings.login_max_attempts, settings.login_lockout_seconds
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    install_session_refresh(app)  # writes the idle-refreshed cookie on authenticated responses
    app.include_router(auth_routes.router)  # PUBLIC: /login, /logout

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: str = Depends(require_session)) -> HTMLResponse:
        # Placeholder protected root (real dashboards land in M7.5+). Exists now so the auth
        # guard is exercised end-to-end.
        return HTMLResponse(
            f"<!DOCTYPE html><html><body><h1>trader monitor</h1>"
            f"<p>signed in as {user}</p></body></html>"
        )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Unauthenticated liveness probe (compose healthcheck). 200 if the read-only DB
        answers a trivial query, else 503 — never raises."""
        try:
            app.state.db.query("SELECT 1")
        except (FileNotFoundError, sqlite3.Error) as exc:
            logger.warning("healthz: state DB unreachable: %s", exc)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"}, status_code=200)

    async def _on_unhandled(request: Request, exc: Exception) -> HTMLResponse:
        # Crash isolation (request level): log the traceback to the web logger and return a
        # generic 500 — never leak internals, never propagate to crash the worker.
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return HTMLResponse(
            "<!DOCTYPE html><html><body><h1>500 — internal error</h1>"
            "<p>The monitor hit an unexpected error. It has been logged.</p></body></html>",
            status_code=500,
        )

    app.add_exception_handler(Exception, _on_unhandled)
    return app


__all__ = ["create_app"]
