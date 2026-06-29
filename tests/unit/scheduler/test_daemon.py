"""Tests for the SchedulerDaemon: job registration, calendar/ledger gating, lock
sharing, and exception isolation (M3.11a). No wall-clock loop — callbacks fired directly."""

import itertools
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import trader.strategy  # noqa: F401 - registers built-in strategies
from fakes import FakeBroker, FakeClock, FakeMarketDataProvider
from trader.config.models import ExecutionConfig, ScheduleConfig
from trader.core import Quote
from trader.core.types import SlotSpec, StrategyBinding
from trader.orchestrator.cycle import CycleResult, Orchestrator
from trader.scheduler.calendar import TradingCalendar
from trader.scheduler.daemon import SchedulerDaemon
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.ledger import FiredSlotLedger
from trader.state.migrate import run_migrations

SESSION = date(2024, 7, 8)
SESSION_NOW = datetime(2024, 7, 8, 14, 0, tzinfo=UTC)  # 10:00 ET on a session
HOLIDAY_NOW = datetime(2024, 12, 25, 15, 0, tzinfo=UTC)


class _SpyOrchestrator:
    def __init__(self, *, errors: list[str] | None = None) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._errors = errors or []

    def run_cycle(
        self, strategy: object, universe: Sequence[str], strategy_id: str, now: datetime
    ) -> CycleResult:
        self.calls.append((strategy_id, now))
        return CycleResult(strategy_id=strategy_id, cycle_id="c", errors=list(self._errors))


def _binding(strategy_id: str, slot_id: str, at: time, *, enabled: bool = True, catch_up=None):
    return StrategyBinding(
        strategy_id=strategy_id,
        strategy_name="threshold",
        params={"band": 0.02, "lot": 10},
        universe=("AAPL",),
        slots=(SlotSpec(slot_id, at, 0, catch_up=catch_up),),
        enabled=enabled,
    )


def _daemon(
    tmp_path: Path, bindings, orchestrator, *, clock: datetime = SESSION_NOW
) -> SchedulerDaemon:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return SchedulerDaemon(
        bindings=bindings,
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        ledger=FiredSlotLedger(conn),
        orchestrator=orchestrator,  # type: ignore[arg-type]
        clock=FakeClock(clock),
        alerter=None,
        sleep=lambda _s: None,  # never block on jitter
    )


def test_one_job_per_slot(tmp_path: Path) -> None:
    bindings = [
        _binding("momentum", "open", time(9, 45)),
        _binding("meanrev", "noon", time(12, 0)),
        _binding("off", "x", time(10, 0), enabled=False),  # disabled -> no job
    ]
    daemon = _daemon(tmp_path, bindings, _SpyOrchestrator())
    daemon.register()
    jobs = {j.id: j for j in daemon.scheduler.get_jobs()}
    assert set(jobs) == {"momentum:open", "meanrev:noon"}
    assert all(j.max_instances == 1 and j.coalesce for j in jobs.values())


def test_misfire_grace_reflects_catch_up(tmp_path: Path) -> None:
    bindings = [
        _binding("a", "yes", time(9, 45), catch_up=True),
        _binding("b", "no", time(10, 0), catch_up=False),
    ]
    daemon = _daemon(tmp_path, bindings, _SpyOrchestrator())
    daemon.register()
    jobs = {j.id: j for j in daemon.scheduler.get_jobs()}
    assert jobs["a:yes"].misfire_grace_time == 120  # schedule default, catch_up True
    assert jobs["b:no"].misfire_grace_time == 1  # catch_up False -> skip stale


def test_calendar_gate_skips(tmp_path: Path) -> None:
    alerts: list[str] = []
    spy = _SpyOrchestrator()
    daemon = _daemon(tmp_path, [_binding("m", "open", time(9, 45))], spy, clock=HOLIDAY_NOW)
    daemon._alert = alerts.append
    assert daemon.fire("m", "open") is None
    assert spy.calls == []  # holiday -> no cycle
    assert alerts and "skipped" in alerts[0]


def test_ledger_blocks_double_fire(tmp_path: Path) -> None:
    spy = _SpyOrchestrator()
    daemon = _daemon(tmp_path, [_binding("m", "open", time(9, 45))], spy)
    daemon.fire("m", "open")
    daemon.fire("m", "open")  # already claimed -> aborts
    assert len(spy.calls) == 1


def test_successful_fire_marks_done(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    ledger = FiredSlotLedger(conn)
    daemon = SchedulerDaemon(
        bindings=[_binding("m", "open", time(9, 45))],
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        ledger=ledger,
        orchestrator=_SpyOrchestrator(),  # type: ignore[arg-type]
        clock=FakeClock(SESSION_NOW),
        sleep=lambda _s: None,
    )
    daemon.fire("m", "open")
    assert ledger.was_fired(SESSION, "m", "open") == "done"


def test_strategy_exception_marks_failed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    ledger = FiredSlotLedger(conn)
    daemon = SchedulerDaemon(
        bindings=[_binding("m", "open", time(9, 45))],
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        ledger=ledger,
        orchestrator=_SpyOrchestrator(errors=["boom"]),  # type: ignore[arg-type]
        clock=FakeClock(SESSION_NOW),
        sleep=lambda _s: None,
    )
    result = daemon.fire("m", "open")  # does not raise
    assert result is not None and result.errors == ["boom"]
    assert ledger.was_fired(SESSION, "m", "open") == "failed"


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


def test_fire_works_off_main_thread(tmp_path: Path) -> None:
    # APScheduler runs callbacks in a worker thread; the SQLite ledger must be usable
    # there (regression for cross-thread access). Run fire() on a separate thread.
    import threading

    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    ledger = FiredSlotLedger(conn)
    daemon = SchedulerDaemon(
        bindings=[_binding("m", "open", time(9, 45))],
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        ledger=ledger,
        orchestrator=_SpyOrchestrator(),  # type: ignore[arg-type]
        clock=FakeClock(SESSION_NOW),
        sleep=lambda _s: None,
    )
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            daemon.fire("m", "open")
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert errors == []  # no "SQLite objects ... same thread" ProgrammingError
    assert ledger.was_fired(SESSION, "m", "open") == "done"


def test_misfire_grace_clamped_when_config_zero(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    daemon = SchedulerDaemon(
        bindings=[_binding("a", "yes", time(9, 45), catch_up=True)],
        schedule=ScheduleConfig(base_seed=42, misfire_grace_seconds=0),  # APScheduler needs >0
        calendar=TradingCalendar(),
        ledger=FiredSlotLedger(conn),
        orchestrator=_SpyOrchestrator(),  # type: ignore[arg-type]
        clock=FakeClock(SESSION_NOW),
        sleep=lambda _s: None,
    )
    daemon.register()  # must not raise (grace clamped to 1)
    assert daemon.scheduler.get_jobs()[0].misfire_grace_time == 1


def test_overlapping_callbacks_share_one_lock(tmp_path: Path) -> None:
    # every slot runs through the SAME orchestrator -> the SAME global lock
    lock = _SpyLock()
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    data = FakeMarketDataProvider(
        quotes={"AAPL": [Quote("AAPL", SESSION_NOW, *([Decimal("100")] * 3), 1000)]}
    )
    ids = (f"o{i}" for i in itertools.count())
    orch = Orchestrator(
        broker=FakeBroker(),
        data=data,
        clock=FakeClock(SESSION_NOW),
        cycle_lock=lock,
        attribution=AttributionLedger(conn),
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids)),
    )
    bindings = [_binding("m", "open", time(9, 45)), _binding("n", "noon", time(12, 0))]
    daemon = _daemon(tmp_path, bindings, orch)
    daemon.fire("m", "open")
    daemon.fire("n", "noon")
    assert lock.enters == 2  # both cycles serialized through the one shared lock
