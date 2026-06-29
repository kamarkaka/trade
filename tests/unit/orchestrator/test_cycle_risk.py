"""M4.4: the real risk gate as the orchestrator's single chokepoint — every order is
checked exactly once before submit, a reject blocks the submit, and same-ticker conflicts
are netted before sizing/submission."""

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeBroker, FakeClock, FakeMarketDataProvider
from trader.clock.virtual import VirtualClock
from trader.config.models import ExecutionConfig, RiskConfig
from trader.core import (
    Account,
    DayState,
    Decision,
    MarketSnapshot,
    Order,
    Position,
    Quote,
    RiskVerdict,
)
from trader.core.enums import Action, Side
from trader.core.protocols import Clock, MarketDataProvider
from trader.orchestrator.cycle import ApproveAllRiskManager, Orchestrator
from trader.orchestrator.lock import GlobalCycleLock, NullLock
from trader.risk.gate import RiskManager
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


def _quote_for(symbol: str) -> Quote:
    p = Decimal("100")
    return Quote(symbol, NOW, p, p, p, 1000, prev_close=p)


class _FillingBroker(FakeBroker):
    """A broker that reflects each fill in its positions on submit, so an order placed
    later in the SAME cycle sees the earlier fill (what SimBroker does; FakeBroker alone
    does not). Lets us prove the orchestrator re-reads state per order."""

    def submit_order(self, order: Order) -> str:
        broker_order_id = super().submit_order(order)
        prior = next((p.quantity for p in self.get_positions() if p.symbol == order.symbol), 0)
        signed = order.quantity if order.side is Side.BUY else -order.quantity
        qty = prior + signed
        price = self.default_fill_price
        self.set_position(Position(order.symbol, qty, price, Decimal(qty) * price))
        return broker_order_id


def _orchestrator(
    tmp_path: Path,
    *,
    risk: object,
    broker: FakeBroker | None = None,
    quotes: dict[str, list[Quote]] | None = None,
) -> tuple[Orchestrator, FakeBroker, AttributionLedger]:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    broker = broker or FakeBroker()
    attribution = AttributionLedger(conn)
    data = FakeMarketDataProvider(quotes=quotes or {"AAPL": [_quote()]})
    ids = (f"o{i}" for i in itertools.count())

    def sizer(decision: Decision, strategy_id: str) -> Order | None:
        return size_decision(decision, strategy_id, ExecutionConfig(), id_factory=lambda: next(ids))

    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=FakeClock(NOW),
        cycle_lock=NullLock(),
        attribution=attribution,
        sizer=sizer,
        risk=risk,  # type: ignore[arg-type]
    )
    return orch, broker, attribution


class _OrderingSpy(ApproveAllRiskManager):
    """Records the interleaving of check() vs submit so we can prove check precedes submit."""

    def __init__(self, log: list[str], approve: bool = True) -> None:
        self._log = log
        self._approve = approve
        self.checks: list[Order] = []

    def check(
        self,
        order: Order,
        positions: Sequence[Position],
        account: Account,
        quote: Quote,
        day_state: DayState,
    ) -> RiskVerdict:
        self._log.append("check")
        self.checks.append(order)
        return RiskVerdict(approved=self._approve)


class _LoggingBroker(FakeBroker):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    def submit_order(self, order: Order) -> str:
        self._log.append("submit")
        return super().submit_order(order)


def test_every_order_passes_risk_check(tmp_path: Path) -> None:
    log: list[str] = []
    spy = _OrderingSpy(log)
    orch, broker, _ = _orchestrator(tmp_path, risk=spy, broker=_LoggingBroker(log))
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert len(spy.checks) == 1  # checked exactly once per order
    assert log == ["check", "submit"]  # and before the submit
    assert len(broker.submitted) == 1


def test_reject_prevents_submit(tmp_path: Path) -> None:
    spy = _OrderingSpy([], approve=False)
    orch, broker, _ = _orchestrator(tmp_path, risk=spy)
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert len(spy.checks) == 1
    assert broker.submitted == []  # rejected -> never submitted
    assert result.fills == []
    assert len(result.rejected) == 1


def test_conflict_netting_applied_before_submit(tmp_path: Path) -> None:
    # The real gate nets a strategy's opposing same-ticker decisions into ONE order.
    gate = RiskManager(account_config=RiskConfig(), clock=VirtualClock(NOW))
    orch, broker, _ = _orchestrator(tmp_path, risk=gate)
    orch.run_cycle(
        _Decide([Decision(Action.BUY, "AAPL", 10), Decision(Action.SELL, "AAPL", 4)]),
        ["AAPL"],
        "m",
        NOW,
    )
    assert len(broker.submitted) == 1  # +10 and -4 collapsed to a single order
    submitted = broker.submitted[0]
    assert submitted.side is Side.BUY and submitted.quantity == 6


def test_conflict_netting_to_flat_submits_nothing(tmp_path: Path) -> None:
    gate = RiskManager(account_config=RiskConfig(), clock=VirtualClock(NOW))
    orch, broker, _ = _orchestrator(tmp_path, risk=gate)
    result = orch.run_cycle(
        _Decide([Decision(Action.BUY, "AAPL", 5), Decision(Action.SELL, "AAPL", 5)]),
        ["AAPL"],
        "m",
        NOW,
    )
    assert broker.submitted == []  # nets to zero -> no order, never cross our own spread
    assert result.fills == [] and result.rejected == []


def test_real_gate_clamp_submits_and_attributes_adjusted_order(tmp_path: Path) -> None:
    # A notional cap clamps the size; the orchestrator must submit AND attribute the ADJUSTED order.
    gate = RiskManager(
        account_config=RiskConfig(max_order_notional_usd=Decimal("500")), clock=VirtualClock(NOW)
    )
    orch, broker, attribution = _orchestrator(tmp_path, risk=gate)
    orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert len(broker.submitted) == 1
    assert broker.submitted[0].quantity == 5  # 10 * 100 > 500 -> clamped to 5
    attributed = attribution.get_attributed("m")
    assert attributed[0].symbol == "AAPL" and attributed[0].quantity == 5  # ledger sees the clamp


def test_intra_cycle_fill_feeds_next_orders_gross_cap(tmp_path: Path) -> None:
    # Two different symbols, each fine alone but together breaching the account-wide gross
    # cap. The orchestrator re-reads positions per order, so the SECOND must be rejected
    # because the FIRST already filled (proves intra-cycle state is threaded into the gate).
    cfg = RiskConfig(
        max_order_notional_usd=Decimal("100000"),  # don't clamp
        max_position_size_pct=100.0,  # don't trip the per-symbol cap
        max_gross_exposure_usd=Decimal("25000"),  # 15k + 15k = 30k > cap
    )
    gate = RiskManager(account_config=cfg, clock=VirtualClock(NOW))
    orch, broker, _ = _orchestrator(
        tmp_path,
        risk=gate,
        broker=_FillingBroker(),
        quotes={"AAPL": [_quote_for("AAPL")], "MSFT": [_quote_for("MSFT")]},
    )
    result = orch.run_cycle(
        _Decide([Decision(Action.BUY, "AAPL", 150), Decision(Action.BUY, "MSFT", 150)]),
        ["AAPL", "MSFT"],
        "m",
        NOW,
    )
    assert [o.symbol for o in broker.submitted] == ["AAPL"]  # AAPL filled; MSFT rejected on gross
    assert [o.symbol for o in result.rejected] == ["MSFT"]


def test_gate_exception_is_isolated(tmp_path: Path) -> None:
    class _Boom(ApproveAllRiskManager):
        def check(self, *a: object, **k: object) -> RiskVerdict:
            raise RuntimeError("gate exploded")

    lock = GlobalCycleLock()
    orch, broker, _ = _orchestrator(tmp_path, risk=_Boom())
    orch._lock = lock  # type: ignore[attr-defined]
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "m", NOW)
    assert result.errors  # caught + recorded, not propagated
    assert broker.submitted == []
    assert lock.acquire(timeout=0.1) is True  # lock released despite the exception
    lock.release()


def test_real_gate_rejects_unpriceable_symbol(tmp_path: Path) -> None:
    # No quote for the decided symbol -> fail closed, no submit.
    gate = RiskManager(account_config=RiskConfig(), clock=VirtualClock(NOW))
    orch, broker, _ = _orchestrator(tmp_path, risk=gate)
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "NOPE", 10)]), ["NOPE"], "m", NOW)
    assert broker.submitted == []
    assert result.missing_symbols == ["NOPE"]
    assert len(result.rejected) == 1
