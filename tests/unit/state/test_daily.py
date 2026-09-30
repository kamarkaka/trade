"""Persisted daily counters (LR6): start-of-day equity per exchange session (survives
restarts), trades today from the durable orders table, and loss today from equity."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.core import Account
from trader.state.daily import DailyCounters
from trader.state.db import connect
from trader.state.migrate import run_migrations

NY = ZoneInfo("America/New_York")
MORNING = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)  # 10:00 EDT, session 2026-06-29


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return conn


def _account(equity: str) -> Account:
    e = Decimal(equity)
    return Account(cash=e, buying_power=e, equity=e)


def _order_row(conn: sqlite3.Connection, cid: str, created: datetime, status: str) -> None:
    ts = created.astimezone(UTC).isoformat()
    conn.execute(
        "INSERT INTO orders (client_order_id, strategy_id, symbol, side, quantity, order_type, "
        "limit_price, tif, status, broker_order_id, created_at, updated_at) "
        "VALUES (?, 's1', 'AAPL', 'BUY', 1, 'MARKET', NULL, 'DAY', ?, NULL, ?, ?)",
        (cid, status, ts, ts),
    )


def test_start_of_day_equity_is_the_first_observation_of_the_session(tmp_path: Path) -> None:
    counters = DailyCounters(_conn(tmp_path), tz=NY)
    first = counters.day_state(_account("10000"), MORNING)
    later = counters.day_state(_account("9700"), MORNING + timedelta(hours=3))
    assert first.start_of_day_equity == later.start_of_day_equity == Decimal("10000")
    assert later.loss_today == Decimal("300") and later.trading_date.isoformat() == "2026-06-29"


def test_gains_are_not_losses(tmp_path: Path) -> None:
    counters = DailyCounters(_conn(tmp_path), tz=NY)
    counters.day_state(_account("10000"), MORNING)
    assert counters.day_state(_account("10250"), MORNING).loss_today == Decimal("0")


def test_start_of_day_equity_survives_a_restart(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    DailyCounters(conn, tz=NY).day_state(_account("10000"), MORNING)
    restarted = DailyCounters(conn, tz=NY)  # a new process over the same durable DB
    state = restarted.day_state(_account("9000"), MORNING + timedelta(hours=1))
    assert state.start_of_day_equity == Decimal("10000") and state.loss_today == Decimal("1000")


def test_each_session_gets_its_own_start_of_day_equity(tmp_path: Path) -> None:
    counters = DailyCounters(_conn(tmp_path), tz=NY)
    counters.day_state(_account("10000"), MORNING)
    next_day = counters.day_state(_account("9500"), MORNING + timedelta(days=1))
    assert next_day.start_of_day_equity == Decimal("9500") and next_day.loss_today == 0


def test_trades_today_counts_this_sessions_orders_in_exchange_time(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    counters = DailyCounters(conn, tz=NY)
    _order_row(conn, "a", MORNING, "WORKING")
    _order_row(conn, "b", datetime(2026, 6, 30, 3, 0, tzinfo=UTC), "unknown")  # 23:00 EDT 6/29
    _order_row(conn, "c", MORNING, "not_placed")  # definitely not placed: not a trade
    _order_row(conn, "d", datetime(2026, 6, 29, 3, 59, tzinfo=UTC), "FILLED")  # 23:59 EDT 6/28
    _order_row(conn, "e", datetime(2026, 6, 30, 4, 0, tzinfo=UTC), "FILLED")  # 00:00 EDT 6/30
    assert counters.day_state(_account("10000"), MORNING).trades_today == 2  # a + b


def test_counters_row_is_kept_current_for_the_web_ui(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    counters = DailyCounters(conn, tz=NY, now=lambda: MORNING)
    _order_row(conn, "a", MORNING, "FILLED")
    counters.day_state(_account("10000"), MORNING)
    counters.day_state(_account("9900"), MORNING)
    row = conn.execute(
        "SELECT trades_today, loss_today, start_of_day_equity FROM daily_counters "
        "WHERE trading_date = '2026-06-29'"
    ).fetchone()
    assert (row[0], Decimal(row[1]), Decimal(row[2])) == (1, Decimal("100"), Decimal("10000"))


def test_kill_switch_flag_is_passed_through(tmp_path: Path) -> None:
    counters = DailyCounters(_conn(tmp_path), tz=NY)
    assert counters.day_state(_account("1"), MORNING, True).kill_switch_engaged is True
