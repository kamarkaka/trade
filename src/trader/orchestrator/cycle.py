"""Orchestrator.run_cycle — the shared decision->sizing->submit cycle (design §4.2/§7.5,
Appendix C).

The SAME cycle runs in backtest and live; only the injected Broker/MarketDataProvider/
Clock differ. The whole critical section runs under one global cycle lock so overlapping
fires never read-modify-write account state on stale balances. This cycle handles ONE
strategy, so its decisions are first reconciled by the risk gate's conflict policy
(netting that single strategy's own opposing same-ticker deltas), then EVERY resulting
order traverses the risk gate (``check``) — the single, non-bypassable chokepoint (§4.1
boundary rule 2) — before the broker. (Cross-strategy netting + pro-rata ``contributors``
attribution of a shared fill is a later milestone; here every fill belongs to this
strategy.) A rejected order is logged, audited,
and skipped; an approved order may be clamped (``adjusted_order``). Each order's intent is
persisted (write-ahead) BEFORE submit so a crash mid-submit is recoverable. Fills are
attributed per strategy. A strategy exception is caught, recorded, and isolated — it never
propagates or blocks other strategies (Appendix C #6).

The injected ``RiskManager`` is the real fail-closed gate (``trader.risk.gate``) in paper/
live; it defaults to a permissive approve-all manager so backtests and M3 callers behave
unchanged until the paper pipeline (M4.7) injects the real one.

SAFETY: M4 uses FakeBroker (tests) or SimBroker (paper) only — no real orders.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from trader.core import (
    Account,
    DayState,
    Decision,
    Fill,
    MarketSnapshot,
    Order,
    Position,
    Quote,
    RiskVerdict,
)
from trader.core.enums import Action, ConflictPolicy
from trader.core.protocols import Broker, Clock, MarketDataProvider, Strategy
from trader.observability.logging import cycle_context, get_logger
from trader.risk.gate import ResolvedDecision
from trader.state.attribution import AttributionLedger

from .lock import CycleLock

Sizer = Callable[[Decision, str], Order | None]


class RiskManager(Protocol):
    """The single chokepoint: composes the risk rules into an approve/clamp/reject verdict
    and reconciles same-ticker conflicts across a cycle's decisions. The real implementation
    is ``trader.risk.gate.RiskManager``."""

    def check(
        self,
        order: Order,
        positions: Sequence[Position],
        account: Account,
        quote: Quote,
        day_state: DayState,
    ) -> RiskVerdict: ...

    def resolve_conflicts(
        self, decisions: Sequence[tuple[str, Decision]], policy: ConflictPolicy | None = None
    ) -> list[ResolvedDecision]: ...


class ApproveAllRiskManager:
    """Permissive default (M3 parity): approves every order and treats each decision
    independently (no netting). Replaced by the real fail-closed gate when the paper
    pipeline (M4.7) injects ``trader.risk.gate.RiskManager``."""

    def check(
        self,
        order: Order,
        positions: Sequence[Position],
        account: Account,
        quote: Quote,
        day_state: DayState,
    ) -> RiskVerdict:
        return RiskVerdict(approved=True)

    def resolve_conflicts(
        self, decisions: Sequence[tuple[str, Decision]], policy: ConflictPolicy | None = None
    ) -> list[ResolvedDecision]:
        resolved: list[ResolvedDecision] = []
        for sid, d in decisions:
            if d.action is Action.HOLD or d.quantity <= 0:
                continue
            signed = d.quantity if d.action is Action.BUY else -d.quantity
            resolved.append(
                ResolvedDecision(d.symbol, d.action, d.quantity, ((sid, signed),), d.limit_price)
            )
        return resolved


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AuditEvent:
    cycle_id: str  # correlation id tying every row of one cycle's chain together
    strategy_id: str
    kind: str  # order_pending | fill | rejected | cycle_error
    detail: str
    payload: Mapping[str, object] = field(default_factory=dict)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class ListAuditSink:
    """In-memory audit sink (default for tests)."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class SqliteAuditSink:
    """Durable audit chain in the ``audit_log`` table (design §12): one JSON row per
    event, correlated by ``cycle_id``, so the inputs->decision->risk->order->fill chain is
    reconstructable. The paper pipeline and live share this schema."""

    def __init__(self, conn: sqlite3.Connection, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._conn = conn
        self._now = now

    def record(self, event: AuditEvent) -> None:
        payload = json.dumps({"detail": event.detail, **dict(event.payload)}, default=str)
        self._conn.execute(
            "INSERT INTO audit_log (ts, cycle_id, strategy_id, kind, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                self._now().astimezone(UTC).isoformat(),
                event.cycle_id,
                event.strategy_id,
                event.kind,
                payload,
            ),
        )


@dataclass
class CycleResult:
    strategy_id: str
    cycle_id: str
    decisions: list[Decision] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    rejected: list[Order] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)


class Orchestrator:
    """Runs one strategy's decision->submit cycle, serialized + risk-gated + attributed."""

    def __init__(
        self,
        *,
        broker: Broker,
        data: MarketDataProvider,
        clock: Clock,
        cycle_lock: CycleLock,
        attribution: AttributionLedger,
        sizer: Sizer,
        risk: RiskManager | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._broker = broker
        self._data = data
        self._clock = clock
        self._lock = cycle_lock
        self._attribution = attribution
        self._sizer = sizer
        self._risk: RiskManager = risk or ApproveAllRiskManager()
        self._audit = audit or ListAuditSink()
        self._log = get_logger("orchestrator")

    def run_cycle(
        self,
        strategy: Strategy,
        universe: Sequence[str],
        strategy_id: str,
        now: datetime,
        day_state: DayState | None = None,
    ) -> CycleResult:
        cycle_id = uuid.uuid4().hex
        result = CycleResult(strategy_id=strategy_id, cycle_id=cycle_id)
        with self._lock, cycle_context(cycle_id):
            try:
                quotes, result.missing_symbols = self._snapshot(universe, now)
                snapshot = MarketSnapshot(asof=now, quotes=quotes)
                positions = self._broker.get_positions()
                account = self._broker.get_account()
                decisions = list(
                    strategy.decide(snapshot, positions, account, self._data, self._clock)
                )
                result.decisions = decisions
                ds = day_state if day_state is not None else self._default_day_state(account, now)
                # Reconcile same-ticker conflicts across the cycle's decisions BEFORE sizing
                # (net default), then route each resulting order through the chokepoint.
                resolved = self._risk.resolve_conflicts([(strategy_id, d) for d in decisions])
                for rd in resolved:
                    self._handle_resolved(rd, strategy_id, cycle_id, snapshot, ds, result)
            except Exception as exc:
                # Strategy isolation (Appendix C#6): a failing cycle must never crash the
                # daemon or block other strategies. exc_info carries the traceback to logs
                # so an orchestrator bug (vs a strategy bug) is still diagnosable. Partial
                # effects (earlier fills already attributed) are intentional — recovery is via
                # reconcile, not rollback. (At-most-once placement via submit_idempotent (M5.3)
                # is wired into this submit path at go-live, M5.6/M5.7; paper submits directly.)
                self._log.error(
                    "cycle failed", strategy_id=strategy_id, error=str(exc), exc_info=True
                )
                self._audit.record(AuditEvent(cycle_id, strategy_id, "cycle_error", str(exc)))
                result.errors.append(str(exc))
        return result

    def _handle_resolved(
        self,
        rd: ResolvedDecision,
        strategy_id: str,
        cycle_id: str,
        snapshot: MarketSnapshot,
        day_state: DayState,
        result: CycleResult,
    ) -> None:
        decision = Decision(rd.action, rd.symbol, rd.quantity, rd.limit_price)
        order = self._sizer(decision, strategy_id)
        if order is None:
            return
        quote = snapshot.quotes.get(order.symbol)
        if quote is None:
            # Fail closed: never trade a symbol we have no quote for (the gate would reject
            # anyway; do it here so check() keeps its non-optional Quote contract).
            self._reject(order, strategy_id, cycle_id, result, "no quote (fail closed)")
            return
        # Re-read account/positions so each order is gated against the post-previous-fill
        # state (the lock guarantees no OTHER cycle interleaves; intra-cycle fills must
        # still count toward the resulting-position caps).
        positions = self._broker.get_positions()
        account = self._broker.get_account()
        verdict = self._risk.check(order, positions, account, quote, day_state)
        if not verdict.approved:
            self._reject(order, strategy_id, cycle_id, result, "; ".join(verdict.reasons))
            return
        final_order = verdict.adjusted_order or order  # honour a risk clamp
        # Write-ahead: persist the intent (with its client_order_id) BEFORE submit so a
        # crash mid-submit is recoverable / idempotent. The payload carries the
        # decision + risk verdict so the audit chain reconstructs inputs->...->order.
        self._audit.record(
            AuditEvent(
                cycle_id,
                strategy_id,
                "order_pending",
                final_order.client_order_id,
                payload={
                    "symbol": final_order.symbol,
                    "side": final_order.side.value,
                    "quantity": final_order.quantity,
                    "order_type": final_order.order_type.value,
                    "rationale": rd.action.value,
                    "clamped_from": order.quantity if final_order is not order else None,
                },
            )
        )
        broker_order_id = self._broker.submit_order(final_order)
        # TODO(M5, §4.2): poll get_order until a terminal status (FILLED/PARTIAL/REJECTED)
        # with a bounded timeout; M4's SimBroker/FakeBroker fill synchronously.
        fill = self._broker.get_order(broker_order_id)
        self._attribution.apply(fill, strategy_id, final_order.side)
        self._audit.record(
            AuditEvent(
                cycle_id,
                strategy_id,
                "fill",
                fill.broker_order_id,
                payload={
                    "symbol": fill.symbol,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "status": fill.status.value,
                },
            )
        )
        result.orders.append(final_order)
        result.fills.append(fill)

    def _reject(
        self, order: Order, strategy_id: str, cycle_id: str, result: CycleResult, reason: str
    ) -> None:
        self._log.info(
            "order rejected by risk gate",
            strategy_id=strategy_id,
            symbol=order.symbol,
            cid=order.client_order_id,
            reason=reason,
        )
        self._audit.record(
            AuditEvent(
                cycle_id,
                strategy_id,
                "rejected",
                order.client_order_id,
                payload={"symbol": order.symbol, "reason": reason},
            )
        )
        result.rejected.append(order)

    @staticmethod
    def _default_day_state(account: Account, now: datetime) -> DayState:
        # Neutral day-state for callers that don't track one yet. loss/trades = 0 means the
        # daily-loss / trade-count rails do NOT trip under this default, so paper mode does
        # not enforce those two account-wide rails; real per-day counters / start-of-day
        # equity (from the daily_counters table) are wired with live trading in M5.
        return DayState(
            trading_date=now.date(),
            start_of_day_equity=account.equity,
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            trades_today=0,
            loss_today=Decimal(0),
        )

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
