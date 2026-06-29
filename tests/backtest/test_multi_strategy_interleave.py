"""Multi-strategy backtest interleave: merged-trigger order, per-strategy attribution,
reproducibility, and no-lookahead (M3.10)."""

import itertools
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pandas as pd

import trader.strategy  # noqa: F401 - registers built-in strategies
from trader.backtest import build_multi_report, run_multi_strategy
from trader.broker import SimBroker
from trader.clock import VirtualClock
from trader.config.models import ExecutionConfig, ScheduleConfig
from trader.core import Account, Decision, Fill, MarketSnapshot, Order, Position
from trader.core.enums import OrderStatus, Side
from trader.core.protocols import Clock, MarketDataProvider
from trader.core.types import SlotSpec, StrategyBinding
from trader.data.cache import ParquetCache
from trader.data.historical import HistoricalDataProvider
from trader.scheduler.calendar import TradingCalendar
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations
from trader.strategy.registry import StrategyRegistry
from trader.strategy.strategies.threshold import ThresholdStrategy

START = date(2024, 7, 8)  # Mon
END = date(2024, 7, 10)
START_TS = datetime(2024, 7, 8, 13, 45, tzinfo=UTC)


def _bars(symbol: str, closes: dict[date, str]) -> pd.DataFrame:
    rows = sorted(closes.items())
    return pd.DataFrame(
        {
            "ts": [datetime(d.year, d.month, d.day, tzinfo=UTC) for d, _ in rows],
            "open": [Decimal(c) for _, c in rows],
            "high": [Decimal(c) for _, c in rows],
            "low": [Decimal(c) for _, c in rows],
            "close": [Decimal(c) for _, c in rows],
            "volume": [10000 for _ in rows],
        }
    )


# Declining ~3%/day so the threshold strategy BUYs each session (07-05 seeds prev_close).
_AAPL = {date(2024, 7, 5): "100", START: "97", date(2024, 7, 9): "94", END: "91"}
_MSFT = {date(2024, 7, 5): "200", START: "194", date(2024, 7, 9): "188", END: "182"}


def _bindings() -> list[StrategyBinding]:
    params = {"band": 0.02, "lot": 10}
    return [
        StrategyBinding(
            strategy_id="momentum",
            strategy_name="threshold",
            params=dict(params),
            universe=("AAPL",),
            slots=(SlotSpec("morning", time(9, 45), 0),),
        ),
        StrategyBinding(
            strategy_id="meanrev",
            strategy_name="threshold",
            params=dict(params),
            universe=("MSFT",),
            slots=(SlotSpec("late", time(10, 15), 0),),  # later -> interleaves after momentum
        ),
    ]


def _run(tmp_path: Path, *, start: date = START, end: date = END):
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", _bars("AAPL", _AAPL))
    cache.write_bars("MSFT", _bars("MSFT", _MSFT))
    clock = VirtualClock(datetime(2024, 7, 3, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=Decimal("1000000"))
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    attribution = AttributionLedger(conn)
    ids = (f"o{i}" for i in itertools.count())

    def sizer(decision: Decision, strategy_id: str) -> Order | None:
        return size_decision(decision, strategy_id, ExecutionConfig(), id_factory=lambda: next(ids))

    return run_multi_strategy(
        bindings=_bindings(),
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        data=data,
        broker=broker,
        attribution=attribution,
        sizer=sizer,
        clock=clock,
        start=start,
        end=end,
    ), attribution


def test_two_strategies_interleaved(tmp_path: Path) -> None:
    result, _ = _run(tmp_path)
    # 3 sessions x 2 strategies; momentum (09:45) fires before meanrev (10:15) each day
    assert [c.strategy_id for c in result.cycle_results] == ["momentum", "meanrev"] * 3


def test_per_strategy_attribution(tmp_path: Path) -> None:
    result, attribution = _run(tmp_path)
    assert attribution.get_attributed("momentum")[0].symbol == "AAPL"
    assert attribution.get_attributed("momentum")[0].quantity == 30  # 3 sessions x lot 10
    assert attribution.get_attributed("meanrev")[0].symbol == "MSFT"
    assert attribution.get_attributed("meanrev")[0].quantity == 30
    report = build_multi_report(result.per_strategy_trades, result.equity_curve, {"seed": 42})
    assert set(report["per_strategy"]) == {"momentum", "meanrev"}
    assert report["per_strategy"]["momentum"]["num_trades"] == 3
    assert report["equity_curve"]  # combined curve present


def test_reproducible_run(tmp_path: Path) -> None:
    a, _ = _run(tmp_path / "a")
    b, _ = _run(tmp_path / "b")
    assert a.equity_curve == b.equity_curve
    assert [c.strategy_id for c in a.cycle_results] == [c.strategy_id for c in b.cycle_results]


def test_no_lookahead_fill_uses_current_bar(tmp_path: Path) -> None:
    result, _ = _run(tmp_path)
    # momentum's first fill is on 07-08 -> price is that session's close (97), never a
    # future bar (94/91). Data is asof-bound, so nothing after fire_ts is visible.
    first_momentum = next(c for c in result.cycle_results if c.strategy_id == "momentum")
    assert first_momentum.fills[0].price == Decimal("97")


def test_build_multi_report_realized_pnl() -> None:
    # a buy@10 / sell@12 roundtrip per strategy -> realized +20; pin the report value
    def _fill(qty: int, price: str) -> Fill:
        return Fill(
            "c", "b", "AAPL", qty, Decimal(price), Decimal("0"), START_TS, OrderStatus.FILLED
        )

    trades = {"s": [(_fill(10, "10"), Side.BUY), (_fill(10, "12"), Side.SELL)]}
    report = build_multi_report(trades, [(START_TS, Decimal("100020"))], {"seed": 1})
    assert report["per_strategy"]["s"]["realized_pnl"] == "20"
    assert report["per_strategy"]["s"]["num_trades"] == 2


class _Boom:
    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        raise RuntimeError("strategy blew up")


def test_strategy_exception_isolated_run_continues(tmp_path: Path) -> None:
    registry = StrategyRegistry()
    registry.register("boom")(_Boom)
    registry.register("threshold")(ThresholdStrategy)
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", _bars("AAPL", _AAPL))
    cache.write_bars("MSFT", _bars("MSFT", _MSFT))
    clock = VirtualClock(datetime(2024, 7, 8, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=Decimal("1000000"))
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    ids = (f"o{i}" for i in itertools.count())

    bindings = [
        StrategyBinding("boomstrat", "boom", {}, ("AAPL",), (SlotSpec("a", time(9, 45), 0),)),
        StrategyBinding(
            "good",
            "threshold",
            {"band": 0.02, "lot": 10},
            ("MSFT",),
            (SlotSpec("b", time(10, 15), 0),),
        ),
    ]
    result = run_multi_strategy(
        bindings=bindings,
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        data=data,
        broker=broker,
        attribution=AttributionLedger(conn),
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids)),
        clock=clock,
        start=START,
        end=END,
        registry=registry,
    )
    boom = [c for c in result.cycle_results if c.strategy_id == "boomstrat"]
    good = [c for c in result.cycle_results if c.strategy_id == "good"]
    assert all(c.errors for c in boom)  # every boom cycle recorded an error
    assert any(c.fills for c in good)  # the good strategy still traded -> run continued


def test_holiday_session_skipped(tmp_path: Path) -> None:
    # window spans July 4 (XNYS holiday): no triggers fire on it
    result, _ = _run(tmp_path, start=date(2024, 7, 3), end=date(2024, 7, 5))
    fired_dates = {ts.date() for ts, _ in result.equity_curve}  # one entry per fired trigger
    assert date(2024, 7, 4) not in fired_dates
    assert fired_dates == {date(2024, 7, 3), date(2024, 7, 5)}
