"""Guard: the monitoring service exposes ONLY GET + the login/logout POSTs, and opens the
state DB through a single read-only handle (M7.10 / §17 exit criteria)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount

import trader.web
from trader.web.app import create_app
from trader.web.db import ReadOnlyStateDB
from trader.web.settings import WebSettings

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _api_routes(app) -> list[APIRoute]:
    """All APIRoutes, recursing into included routers (this FastAPI version wraps them as
    _IncludedRouter rather than flattening into app.routes)."""
    found: list[APIRoute] = []

    def walk(routes: object) -> None:
        for route in routes:  # type: ignore[attr-defined]
            if isinstance(route, APIRoute):
                found.append(route)
            elif hasattr(route, "original_router"):  # _IncludedRouter wrapper
                walk(route.original_router.routes)
            elif not isinstance(route, Mount) and hasattr(route, "routes"):
                walk(route.routes)

    walk(app.routes)
    return found


def _app(tmp_path: Path):
    db = tmp_path / "trader.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return create_app(
        WebSettings(
            admin_user="admin",
            admin_password_hash="$argon2id$dummy",
            session_secret="s",
            db_path=db,
            cookie_secure=False,
        )
    )


def test_only_login_logout_are_post(tmp_path: Path) -> None:
    app = _app(tmp_path)
    mutating: set[tuple[str, str]] = set()
    for route in _api_routes(app):
        for method in route.methods & _MUTATING:
            mutating.add((method, route.path))
    assert mutating == {("POST", "/login"), ("POST", "/logout")}, mutating


def test_healthz_is_get(tmp_path: Path) -> None:
    app = _app(tmp_path)
    healthz = [r for r in _api_routes(app) if r.path == "/healthz"]
    assert healthz and healthz[0].methods == {"GET"}


def test_single_readonly_handle() -> None:
    # The ONLY module under trader.web that opens sqlite3 directly is db.py, and it does so
    # read-only (mode=ro). No other route/module may open its own connection.
    web_dir = Path(trader.web.__file__).resolve().parent
    offenders = [
        py.relative_to(web_dir).as_posix()
        for py in web_dir.rglob("*.py")
        if "sqlite3.connect(" in py.read_text(encoding="utf-8") and py.name != "db.py"
    ]
    assert offenders == [], f"web modules opening sqlite3 directly: {offenders}"
    db_src = (web_dir / "db.py").read_text(encoding="utf-8")
    assert "mode=ro" in db_src
    assert "PRAGMA query_only=ON" in db_src


def test_app_db_handle_is_readonly_instance(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert isinstance(app.state.db, ReadOnlyStateDB)
