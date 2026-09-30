"""Persisted daily counters (LR6): start-of-day equity per (exchange session, equity-source
scope), trades today from the durable orders table, loss today from equity, and a fail-closed
refusal of implausible equity."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.core import Account
from trader.execution import idempotency
from trader.state import daily
from trader.state.daily import DailyCounters
from trader.state.db import connect
from trader.state.migrate import run_migrations

NY = ZoneInfo("America/New_York")
MORNING = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)  # 10:00 EDT, session 2026-06-29


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return conn


def _counters(conn: sqlite3.Connection, scope: str = "live:abc", **kw: object) -> DailyCounters:
    return DailyCounters(conn, tz=NY, scope=scope, **kw)  # type: ignore[arg-type]


def _account(equity: str) -> Account:
    e = Decimal(equity)
    return Account(cash=e, buying_power=e, equity=e)


def _order_row(conn: sqlite3.Connection, cid: str, created: str, status: str) -> None:
    conn.execute(
        "INSERT INTO orders (client_order_id, strategy_id, symbol, side, quantity, order_type, "
        "limit_price, tif, status, broker_order_id, created_at, updated_at) "
        "VALUES (?, 's1', 'AAPL', 'BUY', 1, 'MARKET', NULL, 'DAY', ?, NULL, ?, ?)",
        (cid, status, created, created),
    )


def test_start_of_day_equity_is_the_first_observation_of_the_session(tmp_path: Path) -> None:
    counters = _counters(_conn(tmp_path))
    first = counters.day_state(_account("10000"), MORNING)
    later = counters.day_state(_account("9700"), MORNING + timedelta(hours=3))
    assert first.start_of_day_equity == later.start_of_day_equity == Decimal("10000")
    assert later.loss_today == Decimal("300") and later.trading_date.isoformat() == "2026-06-29"


def test_gains_are_not_losses(tmp_path: Path) -> None:
    counters = _counters(_conn(tmp_path))
    counters.day_state(_account("10000"), MORNING)
    assert counters.day_state(_account("10250"), MORNING).loss_today == Decimal("0")


def test_a_scope_survives_restarts(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _counters(conn).day_state(_account("10000"), MORNING)
    restarted = _counters(conn)  # a new live process, same account scope
    state = restarted.day_state(_account("9000"), MORNING + timedelta(hours=1))
    assert state.start_of_day_equity == Decimal("10000") and state.loss_today == Decimal("1000")


def test_scopes_are_isolated(tmp_path: Path) -> None:
    # A paper process that restarted (SimBroker flat again) or a different account must not
    # inherit another source's start-of-day equity.
    conn = _conn(tmp_path)
    _counters(conn, "paper:run1").day_state(_account("103000"), MORNING)
    fresh = _counters(conn, "paper:run2").day_state(_account("100000"), MORNING)
    assert fresh.start_of_day_equity == Decimal("100000") and fresh.loss_today == 0


def test_each_session_gets_its_own_start_of_day_equity(tmp_path: Path) -> None:
    counters = _counters(_conn(tmp_path))
    counters.day_state(_account("10000"), MORNING)
    next_day = counters.day_state(_account("9500"), MORNING + timedelta(days=1))
    assert next_day.start_of_day_equity == Decimal("9500") and next_day.loss_today == 0


@pytest.mark.parametrize("equity", ["0", "-5"])
def test_implausible_equity_fails_closed_and_is_never_recorded(tmp_path: Path, equity: str) -> None:
    conn = _conn(tmp_path)
    counters = _counters(conn)
    with pytest.raises(ValueError, match="implausible"):
        counters.day_state(Account(Decimal(0), Decimal(0), Decimal(equity)), MORNING)
    assert conn.execute("SELECT COUNT(*) FROM daily_counters").fetchone()[0] == 0
    # a later good reading becomes the start-of-day equity
    assert counters.day_state(_account("10000"), MORNING).start_of_day_equity == Decimal("10000")


def test_trades_today_counts_this_sessions_orders_in_exchange_time(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _order_row(conn, "a", "2026-06-29T14:00:00+00:00", "WORKING")
    _order_row(conn, "b", "2026-06-30T03:00:00+00:00", "unknown")  # 23:00 EDT 6/29
    _order_row(conn, "c", "2026-06-29T14:00:00+00:00", "not_placed")  # not a trade
    _order_row(conn, "d", "2026-06-29T03:59:00+00:00", "FILLED")  # 23:59 EDT 6/28
    _order_row(conn, "e", "2026-06-30T04:00:00+00:00", "FILLED")  # 00:00 EDT 6/30
    _order_row(conn, "f", "2026-06-29T21:30:00-04:00", "FILLED")  # a local offset: 6/29
    _order_row(conn, "g", "2026-06-29T12:00:00Z", "FILLED")  # 'Z' suffix: 08:00 EDT 6/29
    assert _counters(conn).day_state(_account("10000"), MORNING).trades_today == 4  # a b f g


def test_counters_row_is_kept_current_for_the_web_ui(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    counters = _counters(conn, now=lambda: MORNING)
    _order_row(conn, "a", MORNING.isoformat(), "FILLED")
    counters.day_state(_account("10000"), MORNING)
    counters.day_state(_account("9900"), MORNING)
    row = conn.execute(
        "SELECT trades_today, loss_today, start_of_day_equity FROM daily_counters "
        "WHERE trading_date = '2026-06-29' AND scope = 'live:abc'"
    ).fetchone()
    assert (row[0], Decimal(row[1]), Decimal(row[2])) == (1, Decimal("100"), Decimal("10000"))


def test_kill_switch_flag_is_passed_through(tmp_path: Path) -> None:
    counters = _counters(_conn(tmp_path))
    assert counters.day_state(_account("1"), MORNING, True).kill_switch_engaged is True


def test_a_scope_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scope"):
        DailyCounters(_conn(tmp_path), tz=NY, scope="")


def test_not_placed_matches_the_placement_layer() -> None:
    assert daily._NOT_PLACED == idempotency.NOT_PLACED
