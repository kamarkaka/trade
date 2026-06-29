"""Read-only monitoring repository tests (M7.5): safe dicts, secrets structurally excluded.

Seeds the REAL state schema (via run_migrations) + a separate token store carrying a sentinel
token value, then asserts no repo method ever surfaces a secret.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trader.state.db import connect
from trader.state.migrate import run_migrations
from trader.web.db import ReadOnlyStateDB
from trader.web.repository import MonitoringRepo, scrub

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
SENTINEL_ACCESS = "SENTINEL-ACCESS-TOKEN-must-never-appear"
SENTINEL_REFRESH = "SENTINEL-REFRESH-TOKEN-must-never-appear"


def _seed_state(path: Path) -> None:
    conn = connect(path)
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
        "start_of_day_equity, updated_at) VALUES ('2026-06-29',1,'0','100000',?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO kill_switch (id, engaged, reason, source, updated_at) "
        "VALUES (1,0,NULL,NULL,?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO heartbeat (id, last_alive_at, scheduler_state, detail) "
        "VALUES (1,?, 'running','3 jobs')",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO fired_slot (slot_date, strategy_id, slot_id, status, planned_fire_ts, "
        "drift_seconds, seed, claimed_at, finished_at) VALUES "
        "('2026-06-29','momentum','open','done',?,120,7,?,?)",
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) VALUES "
        "(?, 'cyc1','momentum','order_pending',?)",
        (NOW.isoformat(), json.dumps({"symbol": "AAPL", "side": "BUY", "quantity": 10})),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) VALUES "
        "(?, 'cyc2','meanrev','cycle_error',?)",
        (NOW.isoformat(), json.dumps({"detail": "boom"})),
    )
    conn.commit()
    conn.close()


def _seed_token_store(state_db: Path) -> None:
    # The repo reads <state_db>.parent / schwab_token.sqlite — same dir.
    tok = state_db.parent / "schwab_token.sqlite"
    conn = sqlite3.connect(tok)
    conn.execute(
        "CREATE TABLE tokens (id INTEGER PRIMARY KEY, access_token TEXT, refresh_token TEXT, "
        "access_expires_at TEXT, refresh_issued_at TEXT, scope TEXT)"
    )
    conn.execute(
        "INSERT INTO tokens VALUES (1, ?, ?, ?, ?, 'readonly')",
        (
            SENTINEL_ACCESS,
            SENTINEL_REFRESH,
            (NOW + timedelta(minutes=20)).isoformat(),
            (NOW - timedelta(days=2)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def repo(tmp_path: Path) -> MonitoringRepo:
    db = tmp_path / "trader.sqlite"
    _seed_state(db)
    _seed_token_store(db)
    return MonitoringRepo(
        ReadOnlyStateDB(db),
        config_loader=lambda: {
            "mode": "paper",
            "account": {"secrets_ref": "keychain"},
            "session_secret": "should-be-redacted",
            "admin_password_hash": "should-be-redacted",
        },
    )


def _crawl_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_crawl_strings(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_crawl_strings(v))
    elif isinstance(value, str):
        out.append(value)
    return out


def test_token_status_excludes_token_value(repo: MonitoringRepo) -> None:
    status = repo.token_status(NOW)
    assert status["authenticated"] is True
    assert "access_token" not in status and "refresh_token" not in status
    assert status["refresh_token_age_days"] == pytest.approx(2.0)
    assert status["access_token_seconds_remaining"] == pytest.approx(1200.0)
    # the raw token strings appear NOWHERE in the returned structure
    blob = " ".join(_crawl_strings(status))
    assert SENTINEL_ACCESS not in blob and SENTINEL_REFRESH not in blob


def test_token_status_no_token_columns(repo: MonitoringRepo) -> None:
    # Crawl every repo method's output for the sentinel token values (nested/serialized leak).
    everything: list[Any] = [
        repo.system_status(),
        repo.schedule_status(),
        repo.strategy_list(),
        repo.strategy_detail("momentum"),
        repo.recent_decisions(),
        repo.positions_account(),
        repo.positions_attributed(),
        repo.account_summary(),
        repo.pnl_summary(),
        repo.recent_orders(),
        repo.order_fills("c1"),
        repo.recent_alerts(),
        repo.token_status(NOW),
        repo.config_view(),
    ]
    blob = " ".join(s for item in everything for s in _crawl_strings(item))
    assert SENTINEL_ACCESS not in blob
    assert SENTINEL_REFRESH not in blob


def test_token_status_unauthenticated_when_missing(tmp_path: Path) -> None:
    db = tmp_path / "trader.sqlite"
    _seed_state(db)  # no token store written
    repo = MonitoringRepo(ReadOnlyStateDB(db))
    assert repo.token_status(NOW) == {"authenticated": False}


def test_recent_orders_shape(repo: MonitoringRepo) -> None:
    orders = repo.recent_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o["strategy_id"] == "momentum" and o["status"] == "FILLED"
    assert "client_order_id" in o
    fills = repo.order_fills("c1")
    assert fills[0]["fees"] == "0.50"


def test_positions_account_vs_attributed(repo: MonitoringRepo) -> None:
    acct = repo.positions_account()
    attr = repo.positions_attributed()
    assert acct[0]["symbol"] == "AAPL" and acct[0]["market_value"] == "1010.00"
    assert attr[0]["strategy_id"] == "momentum" and attr[0]["symbol"] == "AAPL"


def test_system_status(repo: MonitoringRepo) -> None:
    s = repo.system_status()
    assert s["heartbeat"]["scheduler_state"] == "running"
    assert s["kill_switch"]["engaged"] is False


def test_recent_decisions_and_alerts(repo: MonitoringRepo) -> None:
    decisions = repo.recent_decisions("momentum")
    assert decisions[0]["kind"] == "order_pending"
    assert decisions[0]["payload"]["symbol"] == "AAPL"  # JSON payload parsed
    alerts = repo.recent_alerts()
    assert any(a["kind"] == "cycle_error" for a in alerts)


def test_strategy_list_and_detail(repo: MonitoringRepo) -> None:
    assert "momentum" in repo.strategy_list()
    detail = repo.strategy_detail("momentum")
    assert detail["attributed_positions"][0]["symbol"] == "AAPL"


def test_scrub_redacts_secret_keys() -> None:
    assert scrub({"refresh_token": "x"}) == {"refresh_token": "***"}
    assert scrub({"nested": {"app_secret": "y", "ok": 1}}) == {
        "nested": {"app_secret": "***", "ok": 1}
    }
    assert scrub([{"password": "p"}]) == [{"password": "***"}]
    assert scrub({"symbol": "AAPL"}) == {"symbol": "AAPL"}  # non-secret preserved


def test_config_view_no_secret_values(repo: MonitoringRepo) -> None:
    cfg = repo.config_view()
    assert cfg["mode"] == "paper"
    assert cfg["session_secret"] == "***"
    assert cfg["admin_password_hash"] == "***"
    assert "should-be-redacted" not in " ".join(_crawl_strings(cfg))
