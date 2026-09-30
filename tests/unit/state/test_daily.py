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


def _counters(conn: sqlite3.Connection, scope: str = "live", **kw: object) -> DailyCounters:
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
        "WHERE trading_date = '2026-06-29' AND scope = 'live'"
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


# --- review follow-ups ------------------------------------------------------------ #


def _broker_account(equity: str, sod: str | None) -> Account:
    e = Decimal(equity)
    return Account(e, e, e, start_of_day_equity=Decimal(sod) if sod is not None else None)


def test_the_brokers_start_of_day_equity_wins_over_the_first_cycle(tmp_path: Path) -> None:
    # The broker's figure includes the opening move a 10:00 first cycle would miss.
    conn = _conn(tmp_path)
    counters = _counters(conn)
    state = counters.day_state(_broker_account("9800", "10500"), MORNING)
    assert state.start_of_day_equity == Decimal("10500") and state.loss_today == Decimal("700")
    # A later read without the broker's figure keeps the persisted broker value.
    later = counters.day_state(_account("9900"), MORNING + timedelta(hours=1))
    assert later.start_of_day_equity == Decimal("10500") and later.loss_today == Decimal("600")
    row = conn.execute("SELECT start_of_day_equity FROM daily_counters").fetchone()
    assert Decimal(row[0]) == Decimal("10500")


def test_the_broker_figure_replaces_an_earlier_capture(tmp_path: Path) -> None:
    counters = _counters(_conn(tmp_path))
    counters.day_state(_account("9800"), MORNING)  # captured before the broker reported one
    state = counters.day_state(_broker_account("9800", "10500"), MORNING + timedelta(hours=1))
    assert state.start_of_day_equity == Decimal("10500")
    # ... and it is what later reads (without the broker's figure) keep.
    later = counters.day_state(_account("9800"), MORNING + timedelta(hours=2))
    assert later.start_of_day_equity == Decimal("10500")


@pytest.mark.parametrize("sod", ["0", "-5"])
def test_a_non_positive_broker_figure_is_ignored(tmp_path: Path, sod: str) -> None:
    state = _counters(_conn(tmp_path)).day_state(_broker_account("9800", sod), MORNING)
    assert state.start_of_day_equity == Decimal("9800")  # the capture


@pytest.mark.parametrize(
    ("created_utc", "session"),
    [
        # Fall back (2026-11-01, 02:00 EDT -> 01:00 EST): a 25-hour session.
        ("2026-11-01T03:30:00+00:00", "2026-10-31"),  # 23:30 EDT
        ("2026-11-01T04:30:00+00:00", "2026-11-01"),  # 00:30 EDT
        ("2026-11-02T04:30:00+00:00", "2026-11-01"),  # 23:30 EST
        ("2026-11-02T05:30:00+00:00", "2026-11-02"),  # 00:30 EST
        # Spring forward (2026-03-08, 02:00 EST -> 03:00 EDT): a 23-hour session.
        ("2026-03-08T04:30:00+00:00", "2026-03-07"),  # 23:30 EST
        ("2026-03-08T05:30:00+00:00", "2026-03-08"),  # 00:30 EST
        ("2026-03-09T03:30:00+00:00", "2026-03-08"),  # 23:30 EDT
        ("2026-03-09T04:30:00+00:00", "2026-03-09"),  # 00:30 EDT
    ],
)
def test_sessions_bucket_correctly_across_dst(
    tmp_path: Path, created_utc: str, session: str
) -> None:
    from datetime import date

    conn = _conn(tmp_path)
    _order_row(conn, "o1", created_utc, "FILLED")
    counters = _counters(conn)
    counts = {
        d: counters.trades_on(date.fromisoformat(d))
        for d in (
            "2026-10-31",
            "2026-11-01",
            "2026-11-02",
            "2026-03-07",
            "2026-03-08",
            "2026-03-09",
        )
    }
    assert counts == {d: int(d == session) for d in counts}


def test_migration_006_keeps_existing_counters_under_an_empty_scope(tmp_path: Path) -> None:
    import shutil

    from trader.state.migrate import MIGRATIONS_DIR

    before = tmp_path / "before_006"
    before.mkdir()
    for path in sorted(Path(MIGRATIONS_DIR).glob("*.sql")):
        if path.name < "006":
            shutil.copy(path, before / path.name)
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn, before)
    conn.execute(
        "INSERT INTO daily_counters (trading_date, trades_today, loss_today, "
        "start_of_day_equity, updated_at) VALUES ('2026-06-26', 3, '12.5', '10000', 'x')"
    )
    conn.commit()
    run_migrations(conn)
    rows = conn.execute(
        "SELECT trading_date, scope, trades_today, loss_today, start_of_day_equity "
        "FROM daily_counters"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("2026-06-26", "", 3, "12.5", "10000")]
    # Keyed by (session, scope) now: another scope on the same session is its own row.
    _counters(conn, scope="live").day_state(
        _account("10000"), datetime(2026, 6, 26, 15, tzinfo=UTC)
    )
    assert conn.execute("SELECT COUNT(*) FROM daily_counters").fetchone()[0] == 2


# --- pattern-day-trader inputs (LR8) ---------------------------------------------- #


def _sessions(start, end):  # type: ignore[no-untyped-def]
    """Weekday 'sessions' (no holidays) for tests."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _fill(conn: sqlite3.Connection, cid: str, side: str, ts: str, qty: int = 1) -> None:
    conn.execute(
        "INSERT INTO orders (client_order_id, strategy_id, symbol, side, quantity, order_type, "
        "limit_price, tif, status, broker_order_id, created_at, updated_at) "
        "VALUES (?, 's1', 'AAPL', ?, 1, 'MARKET', NULL, 'DAY', 'FILLED', ?, ?, ?)",
        (cid, side, f"b-{cid}", ts, ts),
    )
    conn.execute(
        "INSERT INTO fills (client_order_id, broker_order_id, symbol, quantity, price, fees, "
        "ts, status) VALUES (?, ?, 'AAPL', ?, '100', '0', ?, 'FILLED')",
        (cid, f"b-{cid}", qty, ts),
    )


def test_pdt_window_is_the_last_n_exchange_sessions(tmp_path: Path) -> None:
    from datetime import date

    conn = _conn(tmp_path)
    counters = DailyCounters(conn, tz=NY, scope="live:x", sessions=_sessions)
    # Mon 2026-06-29: the last 5 weekday sessions are Tue 6/23 .. Mon 6/29
    assert counters.pdt_window_start(date(2026, 6, 29)) == date(2026, 6, 23)
    no_calendar = DailyCounters(conn, tz=NY, scope="live:x")
    assert no_calendar.pdt_window_start(date(2026, 6, 29)) is None


def test_day_state_carries_executions_since_the_window_start(tmp_path: Path) -> None:
    from trader.core.enums import Side

    conn = _conn(tmp_path)
    _fill(conn, "old", "BUY", "2026-06-22T15:00:00+00:00")  # before the window
    _fill(conn, "buy", "BUY", "2026-06-29T14:00:00+00:00")
    _fill(conn, "sell", "SELL", "2026-06-29T15:00:00+00:00")
    _fill(conn, "none", "BUY", "2026-06-29T15:30:00+00:00", qty=0)  # nothing filled
    state = DailyCounters(conn, tz=NY, scope="live:x", sessions=_sessions).day_state(
        _account("10000"), MORNING
    )
    assert state.pdt_window_start is not None and state.pdt_window_start.isoformat() == "2026-06-23"
    assert [(s, side) for s, side, _ in state.executions] == [
        ("AAPL", Side.BUY),
        ("AAPL", Side.SELL),
    ]
