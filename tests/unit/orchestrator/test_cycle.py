"""Tests for Orchestrator.run_cycle: decisions->fills, attribution, the risk
chokepoint, write-ahead ordering, lock usage, and strategy-exception isolation (M3.9c)."""

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeBroker, FakeClock, FakeMarketDataProvider
from trader.config.models import ExecutionConfig
from trader.core import Account, Decision, MarketSnapshot, Order, Position, Quote
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.orchestrator.cycle import Orchestrator
from trader.orchestrator.lock import NullLock
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _quote() -> Quote:
    p = Decimal("100")
    return Quote("AAPL", NOW, p, p, p, 1000, prev_close=p)


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


class _Raise:
    def decide(self, *args: object, **kwargs: object) -> Sequence[Decision]:
        raise RuntimeError("boom")


def _orchestrator(
    tmp_path: Path,
    *,
    broker: FakeBroker | None = None,
    lock: object | None = None,
    risk: object | None = None,
    audit: object | None = None,
) -> tuple[Orchestrator, FakeBroker, AttributionLedger]:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    attribution = AttributionLedger(conn)
    broker = broker or FakeBroker()
    data = FakeMarketDataProvider(quotes={"AAPL": [_quote()]})
    ids = (f"o{i}" for i in itertools.count())

    def sizer(decision: Decision, strategy_id: str) -> Order | None:
        return size_decision(decision, strategy_id, ExecutionConfig(), id_factory=lambda: next(ids))

    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=FakeClock(NOW),
        cycle_lock=lock or NullLock(),
        attribution=attribution,
        sizer=sizer,
        risk=risk,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
    )
    return orch, broker, attribution


def test_decisions_to_fills(tmp_path: Path) -> None:
    orch, broker, _ = _orchestrator(tmp_path)
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "momentum", NOW)
    assert len(result.fills) == 1
    assert result.fills[0].symbol == "AAPL"
    assert len(broker.submitted) == 1
    assert result.errors == []


def test_hold_no_order(tmp_path: Path) -> None:
    orch, broker, _ = _orchestrator(tmp_path)
    result = orch.run_cycle(_Decide([Decision(Action.HOLD, "AAPL")]), ["AAPL"], "m", NOW)
    assert result.orders == []
    assert broker.submitted == []


def test_attribution_per_strategy(tmp_path: Path) -> None:
    orch, _broker, attribution = _orchestrator(tmp_path)
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "momentum", NOW)
    attributed = attribution.get_attributed("momentum")
    assert attributed[0].symbol == "AAPL"
    assert attributed[0].quantity == 10


def test_strategy_exception_isolated(tmp_path: Path) -> None:
    orch, broker, _ = _orchestrator(tmp_path)
    result = orch.run_cycle(_Raise(), ["AAPL"], "m", NOW)  # decide() raises
    assert result.errors  # recorded, not propagated
    assert broker.submitted == []


class _SpyRisk:
    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.calls: list[Order] = []

    def check(self, order: Order) -> bool:
        self.calls.append(order)
        return self.approve


def test_every_order_passes_risk_check(tmp_path: Path) -> None:
    risk = _SpyRisk(approve=True)
    orch, broker, _ = _orchestrator(tmp_path, risk=risk)
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert len(risk.calls) == 1  # checked exactly once, before submit
    assert len(broker.submitted) == 1


def test_risk_reject_prevents_submit(tmp_path: Path) -> None:
    risk = _SpyRisk(approve=False)
    orch, broker, _ = _orchestrator(tmp_path, risk=risk)
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert len(risk.calls) == 1
    assert broker.submitted == []  # rejected -> never submitted
    assert result.fills == []


class _SpyLock:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def acquire(self, timeout: float | None = None) -> bool:
        return True

    def release(self) -> None:
        return None

    def __enter__(self) -> "_SpyLock":
        self._log.append("lock_enter")
        return self

    def __exit__(self, *args: object) -> None:
        self._log.append("lock_exit")


class _SpyBroker(FakeBroker):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    def submit_order(self, order: Order) -> str:
        self._log.append("submit")
        return super().submit_order(order)


class _SpyAudit:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def record(self, event: object) -> None:
        self._log.append(event.kind)  # type: ignore[attr-defined]


def test_pending_before_submit_and_lock_wraps_cycle(tmp_path: Path) -> None:
    log: list[str] = []
    orch, _broker, _ = _orchestrator(
        tmp_path, broker=_SpyBroker(log), lock=_SpyLock(log), audit=_SpyAudit(log)
    )
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert log[0] == "lock_enter"
    assert log[-1] == "lock_exit"
    assert log.index("order_pending") < log.index("submit")  # write-ahead before submit
    assert log.index("submit") < log.index("fill")
