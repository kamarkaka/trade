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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trader.web.db import ReadOnlyStateDB
from trader.web.settings import WebSettings

_WEB_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _WEB_DIR / "static"
_TEMPLATES_DIR = _WEB_DIR / "templates"

logger = logging.getLogger("trader.web")  # web's OWN logger (stdout) — never the trading DB


def create_app(settings: WebSettings) -> FastAPI:
    """Build the read-only monitoring FastAPI app from injected settings."""
    app = FastAPI(title="trader monitor", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = ReadOnlyStateDB(settings.db_path)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

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
