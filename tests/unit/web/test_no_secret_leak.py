"""Guard: NO secret ever appears in ANY response. Seeds sentinel secret values, then crawls
EVERY authenticated GET route (filling path params with seeded ids) and asserts no sentinel
leaks. Also asserts the crawl covers the full GET route set — no route silently skipped.
(M7.10 / §19.6 exit criteria.)"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from trader.state.db import connect
from trader.state.migrate import run_migrations
from trader.web.app import create_app
from trader.web.db import ReadOnlyStateDB
from trader.web.repository import MonitoringRepo
from trader.web.security import make_session_token
from trader.web.settings import WebSettings

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
RT = "SENTINEL-REFRESH-TOKEN"
AT = "SENTINEL-ACCESS-TOKEN"
SESSION_SECRET = "SENTINEL-SESSION-SECRET"
PW_HASH = "SENTINEL-PASSWORD-HASH"
APP_SECRET = "SENTINEL-APP-SECRET"
SENTINELS = (RT, AT, SESSION_SECRET, PW_HASH, APP_SECRET)

# Seeded ids so every path-param GET route is reachable (200), not skipped.
PATH_FILL = {"strategy_id": "momentum", "order_id": "c1"}

_CONFIG = {
    "mode": "paper",
    # Secret-ish keys placed INSIDE rendered sections (risk table + strategy params) so the
    # crawl genuinely exercises scrub: if scrub were a no-op these would leak into the page.
    "risk": {"max_trades_per_day": 6, "api_secret": APP_SECRET},
    "strategies": [
        {
            "id": "momentum",
            "name": "threshold",
            "enabled": True,
            "universe": ["AAPL"],
            "params": {"lot": 10, "client_secret": PW_HASH},
        }
    ],
    # also at the top level / account (defense in depth; not rendered, but must stay absent)
    "session_secret": SESSION_SECRET,
    "account": {"secrets_ref": "keychain", "app_secret": APP_SECRET},
}


def _seed(db: Path) -> None:
    conn = connect(db)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO orders (client_order_id, strategy_id, symbol, side, quantity, order_type, "
        "tif, status, broker_order_id, created_at, updated_at) VALUES "
        "('c1','momentum','AAPL','BUY',10,'MARKET','DAY','FILLED','b1',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO fills (client_order_id, broker_order_id, symbol, quantity, price, fees, "
        "ts, status) VALUES ('c1','b1','AAPL',10,'100.00','0.50',?,'FILLED')",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO kill_switch (id, engaged, updated_at) VALUES (1,0,?)", (NOW.isoformat(),)
    )
    conn.execute(
        "INSERT INTO heartbeat (id, last_alive_at, scheduler_state) VALUES (1,?, 'running')",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO attributed_position (strategy_id, symbol, quantity, avg_price) "
        "VALUES ('momentum','AAPL',10,'100.00')"
    )
    conn.execute(
        "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) VALUES "
        "(?, 'cyc1','momentum','order_pending',?)",
        (NOW.isoformat(), json.dumps({"symbol": "AAPL"})),
    )
    conn.commit()
    conn.close()
    # Token store with sentinel token VALUES — only timestamps may ever be surfaced.
    tok = sqlite3.connect(db.parent / "schwab_token.sqlite")
    tok.execute(
        "CREATE TABLE tokens (id INTEGER PRIMARY KEY, access_token TEXT, refresh_token TEXT, "
        "access_expires_at TEXT, refresh_issued_at TEXT, scope TEXT)"
    )
    tok.execute(
        "INSERT INTO tokens VALUES (1, ?, ?, ?, ?, 's')",
        (AT, RT, (NOW + timedelta(minutes=20)).isoformat(), (NOW - timedelta(days=2)).isoformat()),
    )
    tok.commit()
    tok.close()


def _client(tmp_path: Path) -> TestClient:
    db = tmp_path / "trader.sqlite"
    _seed(db)
    settings = WebSettings(
        admin_user="admin",
        admin_password_hash=PW_HASH,
        session_secret=SESSION_SECRET,
        db_path=db,
        cookie_secure=False,
    )
    app = create_app(settings, now=lambda: NOW)
    app.state.repo = MonitoringRepo(
        ReadOnlyStateDB(db),
        config_loader=lambda: _CONFIG,
        token_store_path=db.parent / "schwab_token.sqlite",
    )
    client = TestClient(app)
    client.cookies.set("session", make_session_token(SESSION_SECRET, "admin", NOW))
    return client, app


def _get_routes(app):
    # Recurse into included routers (this FastAPI version wraps them as _IncludedRouter).
    found = []

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                if "GET" in route.methods:
                    found.append(route)
            elif hasattr(route, "original_router"):  # _IncludedRouter wrapper
                walk(route.original_router.routes)
            elif not isinstance(route, Mount) and hasattr(route, "routes"):
                walk(route.routes)

    walk(app.routes)
    return found


def _concrete(path: str) -> str:
    for k, v in PATH_FILL.items():
        path = path.replace("{" + k + "}", v)
    return path


def test_no_sentinel_secret_in_any_response(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    for route in _get_routes(app):
        if route.path.startswith("/static"):
            continue
        url = _concrete(route.path)
        assert "{" not in url, f"unfilled path param in {route.path}"
        resp = client.get(url)
        # A 500'd secret page would trivially lack the sentinel -> assert it actually rendered
        # so a crashed page can't vacuously "pass" the leak check.
        assert resp.status_code == 200, f"{url} -> {resp.status_code} (secret page must render)"
        for sentinel in SENTINELS:
            assert sentinel not in resp.text, f"{sentinel} leaked at {url}"


def test_crawl_covers_all_get_routes(tmp_path: Path) -> None:
    _, app = _client(tmp_path)
    routes = {r.path for r in _get_routes(app) if not r.path.startswith("/static")}
    # Every GET route is either fillable (no remaining {param}) — i.e. the crawl reaches it.
    for path in routes:
        assert "{" not in _concrete(path), f"crawl cannot reach {path} (unknown path param)"
    # sanity: the detail routes ARE in the set (so they're actually scanned)
    assert "/strategies/{strategy_id}" in routes
    assert "/orders/{order_id}" in routes


def test_token_timestamps_still_render(tmp_path: Path) -> None:
    # Negative control: the token COUNTDOWN renders (so we know the page isn't blank/erroring
    # while the token VALUES are absent).
    client, _ = _client(tmp_path)
    body = client.get("/system").text
    assert "days" in body  # countdown present
    assert RT not in body and AT not in body
