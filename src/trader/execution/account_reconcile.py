"""Account reconciliation: settle every open order row, then true positions (LR9; §10).

``reconcile_account`` drives each unsettled order to a terminal, fully recorded state:

1. a row without a broker id (``pending``/``unknown``) goes through ``resolve`` — adopted
   (FOUND), marked ``not_placed`` (ABSENT), or left unresolved with a reason code;
2. a placed row (``WORKING``, id known) is polled to a terminal status — a remainder is
   cancelled, since nothing may rest untracked — and completed atomically (terminal status
   + fill row + attribution, exactly once);
3. only then are positions trued to the broker (``execution.reconcile``): fills settled above
   are attributed to their strategy before any residual is parked under ``unknown``.

It never places an order. Callers must hold the trading lease (``state.lease``): ``resolve``
treats a ``pending`` row as orphaned, which is only true when no other process can be
sending it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial

from trader.core.protocols import Broker
from trader.execution.idempotency import (
    RETRY_LATER_CODES,
    OrderRepository,
    Reconciler,
    ResolveOutcome,
    resolve,
)
from trader.execution.poller import (
    DEFAULT_RETRYABLE,
    OrderStatusUnavailableError,
    PollPolicy,
    poll_until_terminal,
)
from trader.execution.reconcile import ReconcileReport, reconcile
from trader.observability.logging import get_logger
from trader.state.attribution import AttributionLedger

_log = get_logger("execution.account_reconcile")


class Settlement(StrEnum):
    COMPLETED = "completed"  # terminal status recorded (fill row + attribution if it filled)
    NOT_PLACED = "not_placed"  # confirmed never at the broker
    UNRESOLVED = "unresolved"  # still unknown — see code/detail


@dataclass(frozen=True)
class OrderSettlement:
    client_order_id: str
    outcome: Settlement
    detail: str = ""
    code: str = ""  # for UNRESOLVED: RETRY_LATER_CODES mean "run again after the window"


@dataclass(frozen=True)
class AccountReconcileReport:
    orders: tuple[OrderSettlement, ...]
    positions: ReconcileReport

    @property
    def unresolved(self) -> tuple[OrderSettlement, ...]:
        return tuple(o for o in self.orders if o.outcome is Settlement.UNRESOLVED)

    @property
    def is_clean(self) -> bool:
        return not self.unresolved and self.positions.is_clean

    @property
    def retry_later(self) -> bool:
        """Everything unresolved is only waiting out a consistency window."""
        return bool(self.unresolved) and all(o.code in RETRY_LATER_CODES for o in self.unresolved)


def reconcile_account(
    *,
    broker: Broker,
    repo: OrderRepository,
    attribution: AttributionLedger,
    reconcile_order: Reconciler,
    poll_policy: PollPolicy,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retryable: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
) -> AccountReconcileReport:
    """Settle every open order row, then true positions. Hold the trading lease."""
    settlements: list[OrderSettlement] = []
    for record in repo.open_orders():
        cid = record.client_order_id
        if record.broker_order_id is None:
            result = resolve(repo, record, reconcile=reconcile_order)
            if result.outcome is ResolveOutcome.NOT_PLACED:
                settlements.append(OrderSettlement(cid, Settlement.NOT_PLACED))
                continue
            if result.outcome is ResolveOutcome.UNRESOLVED:
                settlements.append(
                    OrderSettlement(cid, Settlement.UNRESOLVED, result.detail, result.code)
                )
                continue
            adopted = repo.get(cid)
            if adopted is None or adopted.broker_order_id is None:  # pragma: no cover
                settlements.append(OrderSettlement(cid, Settlement.UNRESOLVED, "row vanished"))
                continue
            record = adopted
        broker_order_id = record.broker_order_id
        if broker_order_id is None:  # pragma: no cover - adopted or placed rows carry an id
            continue
        try:
            polled = poll_until_terminal(
                broker,
                broker_order_id,
                poll_policy,
                monotonic=monotonic,
                sleep=sleep,
                retryable=retryable,
            )
        except OrderStatusUnavailableError as exc:
            settlements.append(
                OrderSettlement(cid, Settlement.UNRESOLVED, str(exc), "status_unavailable")
            )
            continue
        if not polled.terminal:
            settlements.append(
                OrderSettlement(
                    cid,
                    Settlement.UNRESOLVED,
                    f"still {polled.fill.status.value} after polling",
                    "not_terminal",
                )
            )
            continue
        fill = replace(polled.fill, client_order_id=cid)
        repo.complete(
            record, fill, partial(attribution.apply, fill, record.strategy_id, record.side)
        )
        settlements.append(
            OrderSettlement(cid, Settlement.COMPLETED, f"{fill.status.value} {fill.quantity}")
        )
        _log.info("reconciled order", cid=cid, status=fill.status.value, filled=fill.quantity)
    positions = reconcile(broker, attribution)
    return AccountReconcileReport(tuple(settlements), positions)


def summary_lines(report: AccountReconcileReport) -> list[str]:
    """A human-readable report for the reconcile command (no account identifiers)."""
    counts = {s: sum(1 for o in report.orders if o.outcome is s) for s in Settlement}
    lines = [
        f"orders: {len(report.orders)} open -> completed {counts[Settlement.COMPLETED]}, "
        f"not placed {counts[Settlement.NOT_PLACED]}, unresolved {counts[Settlement.UNRESOLVED]}"
    ]
    lines += [f"  {o.client_order_id}: unresolved [{o.code}] {o.detail}" for o in report.unresolved]
    discrepancies = report.positions.discrepancies
    if discrepancies:
        lines.append(f"positions: {len(discrepancies)} discrepancy(ies), parked under 'unknown'")
        lines += [
            f"  {d.symbol}: broker {d.broker_qty}, attributed {d.attributed_qty}, "
            f"parked {d.parked_qty}"
            for d in discrepancies
        ]
    else:
        lines.append("positions: clean")
    if report.is_clean:
        verdict = "CLEAN"
    elif report.retry_later and not discrepancies:
        verdict = "NOT CLEAN - run again after the consistency window"
    else:
        verdict = "NOT CLEAN - needs attention"
    lines.append(f"result: {verdict}")
    return lines


__all__ = [
    "AccountReconcileReport",
    "OrderSettlement",
    "Settlement",
    "reconcile_account",
    "summary_lines",
]
