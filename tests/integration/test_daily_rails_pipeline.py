"""LR6 end to end: the real durable executor writes order rows and DailyCounters reads them,
so the real risk gate refuses the (max+1)th entry of the session while still letting an exit
through, and a loss breach blocks new entries."""

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeClock, FakeMarketDataProvider
from trader.broker import SimBroker
from trader.config.models import ExecutionConfig, RiskConfig
from trader.core import Account, Decision, MarketSnapshot, Position, Quote
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.execution.executor import DurableOrderExecutor, in_memory_reconciler
from trader.execution.idempotency import OrderRepository
from trader.execution.poller import PollPolicy
from trader.orchestrator.cycle import Orchestrator
from trader.orchestrator.lock import NullLock
from trader.risk.gate import RiskManager
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.daily import DailyCounters
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Decide:
    def __init__(self, decisions: Sequence[Decision]) -> None:
        self._decisions = decisions

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return self._decisions


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol, NOW, p, p, p, 100_000, prev_close=p)


def _pipeline(tmp_path: Path, quotes: dict[str, list[Quote]], risk_cfg: RiskConfig):  # type: ignore[no-untyped-def]
    clock = FakeClock(NOW)
    data = FakeMarketDataProvider(quotes=quotes)
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))
    attribution = AttributionLedger(conn)
    executor = DurableOrderExecutor(
        broker=broker,
        repo=OrderRepository(conn, now=clock.now),  # rows stamped in the simulated session
        attribution=attribution,
        reconcile=in_memory_reconciler(broker.find_by_client_id),
        poll_policy=PollPolicy(timeout_seconds=0),
    )
    ids = (f"o{i}" for i in itertools.count())
    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=NullLock(),
        attribution=attribution,
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids)),
        risk=RiskManager(account_config=risk_cfg, clock=clock),
        executor=executor,
        day_state_provider=DailyCounters(
            conn, tz=ZoneInfo("America/New_York"), scope="paper:test"
        ).day_state,
    )
    return orch, data, broker


def test_trade_budget_refuses_the_next_entry_but_not_an_exit(tmp_path: Path) -> None:
    quotes = {s: [_quote(s, "100")] for s in ("AAPL", "MSFT", "SPY")}
    orch, _, _ = _pipeline(tmp_path, quotes, RiskConfig(max_trades_per_day=2))
    buys = [Decision(Action.BUY, s, 1) for s in ("AAPL", "MSFT", "SPY")]
    first = orch.run_cycle(_Decide(buys), ["AAPL", "MSFT", "SPY"], "s1", NOW)
    assert [o.symbol for o in first.orders] == ["AAPL", "MSFT"]  # 2 = the budget
    assert [o.symbol for o in first.rejected] == ["SPY"]  # counted from durable rows
    exit_ = orch.run_cycle(_Decide([Decision(Action.SELL, "AAPL", 1)]), ["AAPL"], "s1", NOW)
    assert [o.symbol for o in exit_.orders] == ["AAPL"]  # an exit is never trapped


def test_a_loss_breach_blocks_new_entries(tmp_path: Path) -> None:
    quotes = {"AAPL": [_quote("AAPL", "100")], "MSFT": [_quote("MSFT", "100")]}
    roomy = RiskConfig(  # caps wide enough that the loss rail is the only thing in play
        daily_loss_limit_pct=1,
        max_order_notional_usd=Decimal("100000"),
        max_position_size_pct=100,
        max_gross_exposure_usd=Decimal("1000000"),
    )
    orch, _, _ = _pipeline(tmp_path, quotes, roomy)
    first = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 500)]), ["AAPL"], "s1", NOW)
    assert [o.symbol for o in first.orders] == ["AAPL"]  # SOD 100k; 500 sh @ 100
    quotes["AAPL"] = [_quote("AAPL", "95")]  # the provider reads this dict: AAPL drops 5%
    later = orch.run_cycle(_Decide([Decision(Action.BUY, "MSFT", 1)]), ["MSFT"], "s1", NOW)
    assert [o.symbol for o in later.rejected] == ["MSFT"]  # -2.5% > 1%: entries refused


def test_a_loss_breach_auto_engages_the_kill_switch_and_halts_later_cycles(
    tmp_path: Path,
) -> None:
    from trader.risk.kill_switch import KillSwitch, tripping_day_state

    quotes = {"AAPL": [_quote("AAPL", "100")], "MSFT": [_quote("MSFT", "100")]}
    roomy = RiskConfig(
        daily_loss_limit_pct=1,
        max_order_notional_usd=Decimal("100000"),
        max_position_size_pct=100,
        max_gross_exposure_usd=Decimal("1000000"),
    )
    orch, _, _ = _pipeline(tmp_path, quotes, roomy)
    conn = connect(tmp_path / "state.sqlite")
    switch = KillSwitch(conn)
    counters = orch._day_state_provider
    assert counters is not None
    orch._day_state_provider = tripping_day_state(counters, switch, roomy)
    orch._kill_switch = switch.is_engaged
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 500)]), ["AAPL"], "s1", NOW)
    quotes["AAPL"] = [_quote("AAPL", "95")]  # -2.5% > 1%
    breached = orch.run_cycle(_Decide([Decision(Action.BUY, "MSFT", 1)]), ["MSFT"], "s1", NOW)
    assert breached.rejected and switch.is_engaged() and switch.state().source == "auto"
    halted = orch.run_cycle(_Decide([Decision(Action.SELL, "AAPL", 500)]), ["AAPL"], "s1", NOW)
    assert halted.halted and halted.orders == []  # every later cycle halts until released


def test_after_releasing_a_loss_trip_exits_go_through_but_entries_do_not(
    tmp_path: Path,
) -> None:
    from trader.risk.kill_switch import KillSwitch, tripping_day_state

    quotes = {"AAPL": [_quote("AAPL", "100")], "MSFT": [_quote("MSFT", "100")]}
    roomy = RiskConfig(
        daily_loss_limit_pct=1,
        max_order_notional_usd=Decimal("100000"),
        max_position_size_pct=100,
        max_gross_exposure_usd=Decimal("1000000"),
    )
    orch, _, _ = _pipeline(tmp_path, quotes, roomy)
    switch = KillSwitch(connect(tmp_path / "state.sqlite"))
    counters = orch._day_state_provider
    assert counters is not None
    orch._day_state_provider = tripping_day_state(counters, switch, roomy)
    orch._kill_switch = switch.is_engaged
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 500)]), ["AAPL"], "s1", NOW)
    quotes["AAPL"] = [_quote("AAPL", "95")]  # -2.5% > 1%: trips
    orch.run_cycle(_Decide([Decision(Action.BUY, "MSFT", 1)]), ["MSFT"], "s1", NOW)
    assert switch.is_engaged()
    switch.disengage()  # the operator reviews and releases it
    entry = orch.run_cycle(_Decide([Decision(Action.BUY, "MSFT", 1)]), ["MSFT"], "s1", NOW)
    assert entry.rejected and not switch.is_engaged()  # the loss rule refuses; no re-trip
    exit_ = orch.run_cycle(_Decide([Decision(Action.SELL, "AAPL", 500)]), ["AAPL"], "s1", NOW)
    assert [o.symbol for o in exit_.orders] == ["AAPL"] and not switch.is_engaged()


def test_pdt_blocks_the_fourth_same_day_round_trip_under_25k(tmp_path: Path) -> None:
    # Real executor -> durable fills; DailyCounters (with an exchange calendar) supplies the
    # history; the gate's PDT rule refuses the order that would complete a 4th day-trade.
    from trader.scheduler.calendar import TradingCalendar

    symbols = ("AAPL", "MSFT", "SPY", "QQQ")
    quotes = {s: [_quote(s, "10")] for s in symbols}
    clock = FakeClock(NOW)
    data = FakeMarketDataProvider(quotes=quotes)
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    broker = SimBroker(data, clock, starting_cash=Decimal("20000"))  # under the $25k threshold
    attribution = AttributionLedger(conn)
    executor = DurableOrderExecutor(
        broker=broker,
        repo=OrderRepository(conn, now=clock.now),
        attribution=attribution,
        reconcile=in_memory_reconciler(broker.find_by_client_id),
        poll_policy=PollPolicy(timeout_seconds=0),
    )
    ids = (f"o{i}" for i in itertools.count())
    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=NullLock(),
        attribution=attribution,
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids)),
        risk=RiskManager(account_config=RiskConfig(max_trades_per_day=20), clock=clock),
        executor=executor,
        day_state_provider=DailyCounters(
            conn,
            tz=ZoneInfo("America/New_York"),
            scope="paper:test",
            sessions=TradingCalendar().sessions,
        ).day_state,
    )
    for sym in symbols[:3]:  # three complete round trips today
        orch.run_cycle(_Decide([Decision(Action.BUY, sym, 1)]), [sym], "s1", NOW)
        done = orch.run_cycle(_Decide([Decision(Action.SELL, sym, 1)]), [sym], "s1", NOW)
        assert [o.symbol for o in done.orders] == [sym]
    orch.run_cycle(_Decide([Decision(Action.BUY, "QQQ", 1)]), ["QQQ"], "s1", NOW)
    fourth = orch.run_cycle(_Decide([Decision(Action.SELL, "QQQ", 1)]), ["QQQ"], "s1", NOW)
    assert [o.symbol for o in fourth.rejected] == ["QQQ"]  # would be the 4th day-trade
