"""Per-strategy + account view tests (M7.8): authenticated, seeded DB, controlled config."""

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
    "risk": {"max_trades_per_day": 6, "max_gross_exposure_usd": 25000, "daily_loss_limit_pct": 2},
    "strategies": [
        {
            "id": "momentum",
            "name": "threshold",
            "enabled": True,
            "universe": ["AAPL", "MSFT"],
            "params": {"band": 0.02, "lot": 10},
        }
    ],
}


def _seed(db: Path, *, loss_today: str = "10", sod_equity: str = "100000") -> None:
    conn = connect(db)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO orders (client_order_id, strategy_id, symbol, side, quantity, order_type, "
        "tif, status, broker_order_id, created_at, updated_at) VALUES "
        "('c1','momentum','AAPL','BUY',10,'MARKET','DAY','FILLED','b1',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO positions (symbol, quantity, avg_price, market_value, updated_at) "
        "VALUES ('AAPL',10,'100.00','1010.00',?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO attributed_position (strategy_id, symbol, quantity, avg_price) "
        "VALUES ('momentum','AAPL',10,'100.00')"
    )
    conn.execute(
        "INSERT INTO equity_snapshots (ts, equity, cash, realized_pnl, unrealized_pnl) "
        "VALUES (?, '100500','99490','0','10.00')",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO daily_counters (trading_date, trades_today, loss_today, "
        "start_of_day_equity, updated_at) VALUES ('2026-06-29',1,?,?,?)",
        (loss_today, sod_equity, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO kill_switch (id, engaged, updated_at) VALUES (1,0,?)", (NOW.isoformat(),)
    )
    conn.execute(
        "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) VALUES "
        "(?, 'cyc1','momentum','order_pending',?)",
        (NOW.isoformat(), json.dumps({"symbol": "AAPL", "side": "BUY", "quantity": 10})),
    )
    conn.commit()
    conn.close()


def _build(
    tmp_path: Path, *, authed: bool = True, config: dict = _CONFIG, **seed: str
) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "trader.sqlite"
    _seed(db, **seed)
    settings = WebSettings(
        admin_user="admin",
        admin_password_hash="$argon2id$dummy",
        session_secret=SECRET,
        db_path=db,
        cookie_secure=False,
    )
    app = create_app(settings, now=lambda: NOW)
    app.state.repo = MonitoringRepo(ReadOnlyStateDB(db), config_loader=lambda: config)
    client = TestClient(app, follow_redirects=False)
    if authed:
        client.cookies.set("session", make_session_token(SECRET, "admin", NOW))
    return client


def test_strategy_views_require_auth(tmp_path: Path) -> None:
    client = _build(tmp_path, authed=False)
    assert client.get("/strategies", headers={"accept": "text/html"}).status_code == 303
    assert client.get("/strategies/momentum", headers={"HX-Request": "true"}).status_code == 401
    assert client.get("/account", headers={"accept": "text/html"}).status_code == 303


def test_strategy_list_shows_universe_and_params(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/strategies").text
    assert "momentum" in body
    assert "AAPL, MSFT" in body
    assert "band" in body  # params rendered
    assert "1 / 6" in body  # trades today / limit


def test_strategy_detail_renders_audit_chain(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/strategies/momentum").text
    assert "order_pending" in body  # audit chain kind
    assert "AAPL" in body  # attributed position + payload
    assert "threshold" in body  # binding name


def test_unknown_strategy_404(tmp_path: Path) -> None:
    assert _build(tmp_path).get("/strategies/nope").status_code == 404


def test_null_universe_binding_does_not_500(tmp_path: Path) -> None:
    # A present-but-null universe / minimal binding must render, not crash (join(None)).
    cfg = {
        "mode": "paper",
        "risk": {"max_trades_per_day": 6},
        "strategies": [{"id": "bare", "name": "x", "universe": None}, {"id": "minimal"}],
    }
    client = _build(tmp_path, config=cfg)
    assert client.get("/strategies").status_code == 200
    assert client.get("/strategies/bare").status_code == 200
    assert client.get("/strategies/minimal").status_code == 200


def test_account_shows_positions_and_pnl(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/account").text
    assert "100500" in body  # equity
    assert "1010.00" in body  # position market value
    assert "1010" in body  # gross exposure (sum of |market_value|)


def test_daily_loss_proximity_badge(tmp_path: Path) -> None:
    # loss limit = 100000 * 2% = 2000; loss_today 1900 -> 95% -> amber.
    body = _build(tmp_path / "warn", loss_today="1900").get("/account").text
    assert "badge-warn" in body
    # loss_today 2500 -> over limit -> red.
    body2 = _build(tmp_path / "alert", loss_today="2500").get("/account").text
    assert "badge-alert" in body2


def test_account_fragment_is_partial(tmp_path: Path) -> None:
    resp = _build(tmp_path).get("/account/fragment", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<html" not in resp.text.lower()
    assert "Broker-truth positions" in resp.text


# --- LR6 review follow-ups -------------------------------------------------------- #


def _add_order(db: Path, cid: str, created: str, status: str) -> None:
    conn = connect(db)
    conn.execute(
        "INSERT INTO orders (client_order_id, strategy_id, symbol, side, quantity, order_type, "
        "tif, status, broker_order_id, created_at, updated_at) VALUES "
        "(?,'momentum','AAPL','BUY',1,'MARKET','DAY',?,NULL,?,?)",
        (cid, status, created, created),
    )
    conn.commit()
    conn.close()


def test_trades_today_counts_the_exchange_session_and_skips_not_placed(tmp_path: Path) -> None:
    client = _build(tmp_path)  # seeds c1 (FILLED) at 2026-06-29 08:00 EDT
    db = tmp_path / "trader.sqlite"
    _add_order(db, "np", NOW.isoformat(), "not_placed")  # never at the broker: not a trade
    _add_order(db, "prev", "2026-06-29T03:30:00+00:00", "FILLED")  # 23:30 EDT on the 28th
    _add_order(db, "late", "2026-06-30T03:30:00+00:00", "FILLED")  # 23:30 EDT, still the 29th
    _add_order(db, "later", "2026-06-30T03:45:00+00:00", "FILLED")  # 23:45 EDT, the 29th
    # c1 + late + later (a UTC-date bucket would count c1 + prev instead)
    assert "3 / 6" in client.get("/strategies").text


def test_account_page_says_which_session_the_counters_are_for(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/account").text
    assert "session 2026-06-29" in body and "not today" not in body


def test_account_page_flags_counters_from_an_earlier_session(tmp_path: Path) -> None:
    client = _build(tmp_path)
    conn = connect(tmp_path / "trader.sqlite")
    conn.execute("UPDATE daily_counters SET trading_date = '2026-06-26'")
    conn.commit()
    conn.close()
    body = client.get("/account").text
    assert "session 2026-06-26" in body and "not today&#39;s session" in body


def test_the_web_not_placed_status_matches_the_placement_layer() -> None:
    from trader.execution.idempotency import NOT_PLACED
    from trader.web import repository

    assert repository._NOT_PLACED == NOT_PLACED
