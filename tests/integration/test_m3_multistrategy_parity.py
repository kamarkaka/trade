"""M3 exit-criteria parity: the merged-trigger backtest walk and the daemon callbacks
both feed the IDENTICAL run_cycle, so for the same fixture + seed they dispatch the
same strategy per trigger and produce the same per-strategy attribution (M3.12).
SimBroker only — no real orders."""

import itertools
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pandas as pd

import trader.strategy  # noqa: F401 - registers built-in strategies
from fakes import FakeClock
from trader.backtest import run_multi_strategy
from trader.broker import SimBroker
from trader.clock import VirtualClock
from trader.config.models import ExecutionConfig, ScheduleConfig
from trader.core.types import SlotSpec, StrategyBinding
from trader.data.cache import ParquetCache
from trader.data.historical import HistoricalDataProvider
from trader.orchestrator.cycle import CycleResult, ListAuditSink, Orchestrator
from trader.orchestrator.lock import GlobalCycleLock
from trader.scheduler.calendar import TradingCalendar
from trader.scheduler.daemon import SchedulerDaemon
from trader.scheduler.triggers import SlotScheduler
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.ledger import FiredSlotLedger
from trader.state.migrate import run_migrations

START = date(2024, 7, 8)
END = date(2024, 7, 9)
SEED = 42
# threshold (slots A/B on AAPL) + zscore_revert (slot C on MSFT), per the §11 example.
_AAPL = {date(2024, 7, 5): "100", START: "97", END: "94"}  # declining -> threshold BUYs
_MSFT = {date(2024, 7, 5): "200", START: "194", END: "188"}


def _bars(closes: dict[date, str]) -> pd.DataFrame:
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


def _bindings() -> list[StrategyBinding]:
    return [
        StrategyBinding(
            "momentum",
            "threshold",
            {"band": 0.02, "lot": 10},
            ("AAPL",),
            (SlotSpec("a", time(9, 45), 0), SlotSpec("b", time(12, 30), 0)),
        ),
        StrategyBinding(
            "meanrev", "zscore_revert", {}, ("MSFT",), (SlotSpec("c", time(10, 15), 0),)
        ),
    ]


def _cache(root: Path) -> ParquetCache:
    cache = ParquetCache(root)
    cache.write_bars("AAPL", _bars(_AAPL))
    cache.write_bars("MSFT", _bars(_MSFT))
    return cache


def _sizer(ids):
    return lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids))


class _RecordingOrchestrator:
    """Wraps a real Orchestrator, recording the (strategy_id, now) dispatch sequence."""

    def __init__(self, inner: Orchestrator) -> None:
        self._inner = inner
        self.dispatched: list[tuple[str, datetime]] = []

    def run_cycle(
        self, strategy: object, universe: Sequence[str], strategy_id: str, now: datetime
    ) -> CycleResult:
        self.dispatched.append((strategy_id, now))
        return self._inner.run_cycle(strategy, universe, strategy_id, now)  # type: ignore[arg-type]


def _backtest_path(tmp: Path):
    cache = _cache(tmp)
    clock = VirtualClock(datetime(2024, 7, 5, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=Decimal("1000000"))
    conn = connect(tmp / "state.sqlite")
    run_migrations(conn)
    attribution = AttributionLedger(conn)
    result = run_multi_strategy(
        bindings=_bindings(),
        schedule=ScheduleConfig(base_seed=SEED),
        calendar=TradingCalendar(),
        data=data,
        broker=broker,
        attribution=attribution,
        sizer=_sizer(itertools.count()),
        clock=clock,
        start=START,
        end=END,
    )
    dispatch = [
        (c.strategy_id, ts)
        for c, (ts, _eq) in zip(result.cycle_results, result.equity_curve, strict=True)
    ]
    return dispatch, attribution


def _paper_path(tmp: Path):
    cache = _cache(tmp)
    clock = FakeClock(datetime(2024, 7, 5, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=Decimal("1000000"))
    conn = connect(tmp / "state.sqlite")
    run_migrations(conn)
    attribution = AttributionLedger(conn)
    audit = ListAuditSink()
    inner = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=GlobalCycleLock(),
        attribution=attribution,
        sizer=_sizer(itertools.count()),
        audit=audit,
    )
    recording = _RecordingOrchestrator(inner)
    calendar = TradingCalendar()
    bindings = _bindings()
    daemon = SchedulerDaemon(
        bindings=bindings,
        schedule=ScheduleConfig(base_seed=SEED),
        calendar=calendar,
        ledger=FiredSlotLedger(conn),
        orchestrator=recording,
        clock=clock,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    # drive the daemon callbacks in the SAME merged-trigger order the live cron would,
    # advancing the fake clock to each fire instant (no wall-clock).
    scheduler = SlotScheduler(bindings, calendar, SEED)
    for session in calendar.sessions(START, END):
        for trigger in scheduler.triggers_for(session):
            clock.set(trigger.fire_ts)
            daemon.fire(trigger.strategy_id, trigger.slot_id)
    return recording.dispatched, attribution, audit


def test_correct_dispatch(tmp_path: Path) -> None:
    bt, _ = _backtest_path(tmp_path / "bt")
    paper, _, _ = _paper_path(tmp_path / "paper")
    # per session sorted by fire_ts: momentum/a (09:45), meanrev/c (10:15), momentum/b (12:30)
    assert [sid for sid, _ in bt] == ["momentum", "meanrev", "momentum"] * 2
    assert [sid for sid, _ in paper] == ["momentum", "meanrev", "momentum"] * 2


def test_dispatch_sequence_parity(tmp_path: Path) -> None:
    bt, _ = _backtest_path(tmp_path / "bt")
    paper, _, _ = _paper_path(tmp_path / "paper")
    assert bt == paper  # identical (strategy_id, fire_ts) sequence across both code paths


def test_backtest_paper_attribution_parity(tmp_path: Path) -> None:
    _, bt_attr = _backtest_path(tmp_path / "bt")
    _, paper_attr, _ = _paper_path(tmp_path / "paper")
    for sid in ("momentum", "meanrev"):
        assert bt_attr.get_attributed(sid) == paper_attr.get_attributed(sid)
    assert bt_attr.get_attributed("momentum")[0].quantity == 40  # 2 slots x 2 sessions x lot 10


def test_audit_has_per_strategy_rows(tmp_path: Path) -> None:
    _, _, audit = _paper_path(tmp_path / "paper")
    strategy_ids = {e.strategy_id for e in audit.events}
    assert "momentum" in strategy_ids  # threshold traded -> order_pending/fill rows tagged


class _SpyLock:
    def __init__(self) -> None:
        self.enters = 0

    def acquire(self, timeout: float | None = None) -> bool:
        return True

    def release(self) -> None:
        return None

    def __enter__(self) -> "_SpyLock":
        self.enters += 1
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_overlapping_fires_serialize_and_order(tmp_path: Path) -> None:
    # two strategies sharing the SAME fire_ts -> stable (strategy_id, slot_id) tie-break,
    # and both cycles serialize through the one global lock.
    cache = _cache(tmp_path)
    clock = FakeClock(datetime(2024, 7, 5, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    lock = _SpyLock()
    inner = Orchestrator(
        broker=SimBroker(data, clock, starting_cash=Decimal("1000000")),
        data=data,
        clock=clock,
        cycle_lock=lock,
        attribution=AttributionLedger(conn),
        sizer=_sizer(itertools.count()),
    )
    recording = _RecordingOrchestrator(inner)
    calendar = TradingCalendar()
    # both strategies fire at 10:00 exactly (drift 0) -> identical fire_ts
    bindings = [
        StrategyBinding(
            "bbb",
            "threshold",
            {"band": 0.02, "lot": 10},
            ("AAPL",),
            (SlotSpec("x", time(10, 0), 0),),
        ),
        StrategyBinding(
            "aaa",
            "threshold",
            {"band": 0.02, "lot": 10},
            ("MSFT",),
            (SlotSpec("x", time(10, 0), 0),),
        ),
    ]
    daemon = SchedulerDaemon(
        bindings=bindings,
        schedule=ScheduleConfig(base_seed=SEED),
        calendar=calendar,
        ledger=FiredSlotLedger(conn),
        orchestrator=recording,
        clock=clock,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    scheduler = SlotScheduler(bindings, calendar, SEED)
    triggers = scheduler.triggers_for(START)
    assert triggers[0].fire_ts == triggers[1].fire_ts  # equal fire_ts
    assert (triggers[0].strategy_id, triggers[1].strategy_id) == ("aaa", "bbb")  # tie-break
    for trigger in triggers:
        clock.set(trigger.fire_ts)
        daemon.fire(trigger.strategy_id, trigger.slot_id)
    assert lock.enters == 2  # both cycles ran through the one shared lock
