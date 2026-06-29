"""Orders / Alerts / Config view tests (M7.9): authenticated, seeded DB + controlled config."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from trader.state.db import connect
from trader.state.migrate import run_migrations
from trader.web.app import create_app
from trader.web.db import ReadOnlyStateDB
from trader.web.repository import MonitoringRepo
from trader.web.security import make_session_token
from trader.web.settings import WebSettings

SECRET = "view-secret"
NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)

_CONFIG = {
    "mode": "paper",
    "risk": {"max_gross_exposure_usd": 25000, "daily_loss_limit_pct": 2},
    "execution": {"order_type": "market"},
    "alerting": {"channels": ["telegram", "email"], "heartbeat_minutes": 60},
    "strategies": [{"id": "momentum", "name": "threshold", "enabled": True, "universe": ["AAPL"]}],
    "account": {"secrets_ref": "keychain"},
    "session_secret": "MUST-NOT-RENDER",
    "admin_password_hash": "MUST-NOT-RENDER",
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
        "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) VALUES "
        "(?, 'cyc1','meanrev','rejected',?)",
        (NOW.isoformat(), json.dumps({"symbol": "TSLA", "reason": "denylist: TSLA"})),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) VALUES "
        "(?, 'cyc2','momentum','kill_switch_halt',?)",
        (NOW.isoformat(), json.dumps({"detail": "engaged"})),
    )
    conn.commit()
    conn.close()


def _build(tmp_path: Path, *, authed: bool = True) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "trader.sqlite"
    _seed(db)
    settings = WebSettings(
        admin_user="admin",
        admin_password_hash="$argon2id$dummy",
        session_secret=SECRET,
        db_path=db,
        cookie_secure=False,
    )
    app = create_app(settings, now=lambda: NOW)
    app.state.repo = MonitoringRepo(ReadOnlyStateDB(db), config_loader=lambda: _CONFIG)
    client = TestClient(app, follow_redirects=False)
    if authed:
        client.cookies.set("session", make_session_token(SECRET, "admin", NOW))
    return client


def test_orders_lists_status_and_rejection(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/orders").text
    assert "FILLED" in body
    assert "c1" in body
    assert "denylist: TSLA" in body  # rejection reason surfaced


def test_order_detail_shows_fills(tmp_path: Path) -> None:
    body = _build(tmp_path / "a").get("/orders/c1").text
    assert "100.00" in body and "0.50" in body  # fill price + fees
    assert _build(tmp_path / "b").get("/orders/nope").status_code == 404


def test_alerts_lists_recent(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/alerts").text
    assert "rejected" in body
    assert "kill_switch_halt" in body


def test_config_view_scrubs_secrets(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/config").text
    assert "max_gross_exposure_usd" in body  # risk limits shown
    assert "momentum" in body  # strategies shown
    assert "telegram" in body  # alerting channels shown
    assert "MUST-NOT-RENDER" not in body  # session_secret / password hash scrubbed


def test_config_view_requires_auth(tmp_path: Path) -> None:
    client = _build(tmp_path, authed=False)
    assert client.get("/config", headers={"accept": "text/html"}).status_code == 303
    assert client.get("/orders", headers={"HX-Request": "true"}).status_code == 401
    assert client.get("/alerts", headers={"accept": "text/html"}).status_code == 303


def test_orders_fragment_is_partial(tmp_path: Path) -> None:
    resp = _build(tmp_path).get("/orders/fragment", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<html" not in resp.text.lower()
    assert "Recent orders" in resp.text
