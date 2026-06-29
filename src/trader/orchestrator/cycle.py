"""Orchestrator.run_cycle — the shared decision->sizing->submit cycle (design §4.2/§7.5,
Appendix C).

The SAME cycle runs in backtest and live; only the injected Broker/MarketDataProvider/
Clock differ. The whole critical section runs under one global cycle lock so overlapping
fires never read-modify-write account state on stale balances. Every order passes a
risk gate (a no-op approve-all in M3; M5 swaps the real chokepoint) and its intent is
persisted (write-ahead) BEFORE submit so a crash mid-submit is recoverable. Fills are
attributed per strategy. A strategy exception is caught, recorded, and isolated — it
never propagates or blocks other strategies (Appendix C #6).

SAFETY: M3 uses FakeBroker (tests) or SimBroker (paper) only — no real orders.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trader.core import Decision, Fill, MarketSnapshot, Order, Quote
from trader.core.protocols import Broker, Clock, MarketDataProvider, Strategy
from trader.observability.logging import cycle_context, get_logger
from trader.state.attribution import AttributionLedger

from .lock import CycleLock

Sizer = Callable[[Decision, str], Order | None]


class RiskGate(Protocol):
    """The single chokepoint every order traverses before submit (M5 swaps the impl)."""

    def check(self, order: Order) -> bool: ...


class ApproveAllRiskGate:
    """M3 passthrough: approves every order. Replaced by the real risk gate in M5."""

    def check(self, order: Order) -> bool:
        return True


@dataclass(frozen=True)
class AuditEvent:
    cycle_id: str
    strategy_id: str
    kind: str  # order_pending | fill | rejected | cycle_error
    detail: str


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class ListAuditSink:
    """In-memory audit sink (default); the SQLite-backed audit chain is wired in M4."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class CycleResult:
    strategy_id: str
    cycle_id: str
    decisions: list[Decision] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)


class Orchestrator:
    """Runs one strategy's decision->submit cycle, serialized + attributed + audited."""

    def __init__(
        self,
        *,
        broker: Broker,
        data: MarketDataProvider,
        clock: Clock,
        cycle_lock: CycleLock,
        attribution: AttributionLedger,
        sizer: Sizer,
        risk: RiskGate | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._broker = broker
        self._data = data
        self._clock = clock
        self._lock = cycle_lock
        self._attribution = attribution
        self._sizer = sizer
        self._risk = risk or ApproveAllRiskGate()
        self._audit = audit or ListAuditSink()
        self._log = get_logger("orchestrator")

    def run_cycle(
        self, strategy: Strategy, universe: Sequence[str], strategy_id: str, now: datetime
    ) -> CycleResult:
        cycle_id = uuid.uuid4().hex
        result = CycleResult(strategy_id=strategy_id, cycle_id=cycle_id)
        with self._lock, cycle_context(cycle_id):
            try:
                quotes, result.missing_symbols = self._snapshot(universe, now)
                snapshot = MarketSnapshot(asof=now, quotes=quotes)
                decisions = list(
                    strategy.decide(
                        snapshot,
                        self._broker.get_positions(),
                        self._broker.get_account(),
                        self._data,
                        self._clock,
                    )
                )
                result.decisions = decisions
                for decision in decisions:
                    self._handle_decision(decision, strategy_id, cycle_id, result)
            except Exception as exc:
                # Strategy isolation (Appendix C#6): a failing cycle must never crash the
                # daemon or block other strategies. exc_info carries the traceback to logs
                # so an orchestrator bug (vs a strategy bug) is still diagnosable. Partial
                # effects (earlier fills already attributed) are intentional — each order is
                # write-ahead-logged + idempotent, so recovery is via reconcile, not rollback.
                self._log.error(
                    "cycle failed", strategy_id=strategy_id, error=str(exc), exc_info=True
                )
                self._audit.record(AuditEvent(cycle_id, strategy_id, "cycle_error", str(exc)))
                result.errors.append(str(exc))
        return result

    def _handle_decision(
        self, decision: Decision, strategy_id: str, cycle_id: str, result: CycleResult
    ) -> None:
        order = self._sizer(decision, strategy_id)
        if order is None:
            return
        if not self._risk.check(order):  # the single chokepoint before submit
            self._audit.record(AuditEvent(cycle_id, strategy_id, "rejected", order.client_order_id))
            return
        # Write-ahead: persist the intent (with its client_order_id) BEFORE submit so a
        # crash mid-submit is recoverable / idempotent.
        self._audit.record(
            AuditEvent(cycle_id, strategy_id, "order_pending", order.client_order_id)
        )
        broker_order_id = self._broker.submit_order(order)
        # TODO(M5, §4.2): poll get_order until a terminal status (FILLED/PARTIAL/REJECTED)
        # with a bounded timeout; M3's SimBroker/FakeBroker fill synchronously.
        fill = self._broker.get_order(broker_order_id)
        self._attribution.apply(fill, strategy_id, order.side)
        self._audit.record(AuditEvent(cycle_id, strategy_id, "fill", fill.broker_order_id))
        result.orders.append(order)
        result.fills.append(fill)

    def _snapshot(
        self, universe: Sequence[str], now: datetime
    ) -> tuple[dict[str, Quote], list[str]]:
        quotes: dict[str, Quote] = {}
        missing: list[str] = []
        for symbol in universe:
            try:
                quotes[symbol] = self._data.get_quote(symbol, now)
            except (LookupError, ValueError):
                missing.append(symbol)
        return quotes, missing
