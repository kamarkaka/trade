"""Account reconciliation: settle every open order row, then check positions (LR9; §10).

``reconcile_account`` drives each unsettled order to a terminal, fully recorded state:

1. a row without a broker id (``pending``/``unknown``) goes through ``resolve`` — adopted
   (FOUND), marked ``not_placed`` (ABSENT), or left unresolved with a reason code;
2. a placed row (``WORKING``, id known) is read once first: an order that cannot be this
   row's (another symbol, more shares filled than ordered — e.g. a wrong id bound by hand) or
   whose status can't be read is left untouched (unresolved) rather than polled, which could
   cancel it. Otherwise it is polled to a terminal status — a remainder is cancelled, since
   nothing may rest untracked — and completed atomically (terminal status + fill row +
   attribution, exactly once); a fill the repository refuses leaves it unresolved;
3. only then are positions compared with the broker (``execution.reconcile``), so fills
   settled above are attributed to their strategy first; what the broker holds beyond that
   is checked against the operator-acknowledged baseline, which reconciliation never moves
   — only ``accept_positions`` (the operator, with nothing unresolved) does.

It never places an order. Callers must hold the trading lease (``state.lease``) — checked:
``resolve`` treats a ``pending`` row as orphaned, which is only true when no other process
can be sending it.
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
    OrderRecord,
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
from trader.execution.reconcile import ReconcileReport, accept, reconcile
from trader.observability.logging import get_logger
from trader.state.attribution import AttributionLedger, BaselineChange
from trader.state.lease import TradingLease

_log = get_logger("execution.account_reconcile")

_PRECHECK_READS = 3  # transient failures reading a bound order before it is polled


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
    lease: TradingLease,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retryable: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
) -> AccountReconcileReport:
    """Settle every open order row, then true positions. ``lease`` must be held."""
    if not lease.held:
        raise RuntimeError("reconcile_account needs the trading lease held (state.lease)")
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
        settlements.append(
            _settle_placed(
                broker,
                repo,
                attribution,
                record,
                poll_policy,
                monotonic=monotonic,
                sleep=sleep,
                retryable=retryable,
            )
        )
    positions = reconcile(broker, attribution)
    return AccountReconcileReport(tuple(settlements), positions)


def _settle_placed(
    broker: Broker,
    repo: OrderRepository,
    attribution: AttributionLedger,
    record: OrderRecord,
    poll_policy: PollPolicy,
    *,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    retryable: tuple[type[BaseException], ...],
) -> OrderSettlement:
    """Poll a bound row to a terminal status and complete it (see the module docstring)."""
    cid, broker_order_id = record.client_order_id, record.broker_order_id
    if broker_order_id is None:  # pragma: no cover - callers pass bound rows only
        return OrderSettlement(cid, Settlement.UNRESOLVED, "no broker id", "invalid_state")
    refused = _precheck(broker, record, broker_order_id, poll_policy, sleep, retryable)
    if refused is not None:
        return refused
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
        return OrderSettlement(cid, Settlement.UNRESOLVED, str(exc), "status_unavailable")
    if not polled.terminal:
        return OrderSettlement(
            cid,
            Settlement.UNRESOLVED,
            f"still {polled.fill.status.value} after polling",
            "not_terminal",
        )
    fill = replace(polled.fill, client_order_id=cid)
    try:
        newly = repo.complete(
            record, fill, partial(attribution.apply, fill, record.strategy_id, record.side)
        )
    except (ValueError, LookupError) as exc:  # the fill contradicts the row: never attribute
        _log.error("terminal fill refused; not recorded", cid=cid, error=str(exc))
        return OrderSettlement(cid, Settlement.UNRESOLVED, f"fill refused: {exc}", "fill_refused")
    if not newly:
        _log.info("order was already completed", cid=cid)
        return OrderSettlement(cid, Settlement.COMPLETED, "already completed")
    _log.info("reconciled order", cid=cid, status=fill.status.value, filled=fill.quantity)
    return OrderSettlement(cid, Settlement.COMPLETED, f"{fill.status.value} {fill.quantity}")


def _precheck(
    broker: Broker,
    record: OrderRecord,
    broker_order_id: str,
    poll_policy: PollPolicy,
    sleep: Callable[[float], None],
    retryable: tuple[type[BaseException], ...],
) -> OrderSettlement | None:
    """Read the bound order once before polling it (polling may cancel it). Returns the
    UNRESOLVED settlement when it can't be this row's order or can't be read; None to go on."""
    cid = record.client_order_id
    reads = 0
    while True:
        reads += 1
        try:
            fill = broker.get_order(broker_order_id)
        except retryable as exc:
            if reads < _PRECHECK_READS:
                sleep(poll_policy.interval_seconds)
                continue
            return _unreadable(cid, exc)
        except Exception as exc:  # won't clear by waiting
            return _unreadable(cid, exc)
        break
    problems = []
    if fill.broker_order_id != broker_order_id:
        problems.append(f"the broker answered for {fill.broker_order_id}")
    if _canon(fill.symbol) != _canon(record.symbol):
        problems.append(f"symbol {fill.symbol!r}, ordered {record.symbol!r}")
    if fill.quantity > record.quantity:
        problems.append(f"filled {fill.quantity}, ordered {record.quantity}")
    if not problems:
        return None
    _log.error("bound order does not match its row; left untouched", cid=cid)
    detail = f"bound order {broker_order_id} does not match: " + "; ".join(problems)
    return OrderSettlement(cid, Settlement.UNRESOLVED, detail, "bound_order_mismatch")


def _unreadable(cid: str, exc: BaseException) -> OrderSettlement:
    _log.warning("bound order unreadable; left untouched", cid=cid, error=type(exc).__name__)
    detail = f"status read failed ({type(exc).__name__})"
    return OrderSettlement(cid, Settlement.UNRESOLVED, detail, "status_unavailable")


def _canon(symbol: str) -> str:
    return symbol.strip().upper().replace("/", ".")


def accept_positions(
    report: AccountReconcileReport, attribution: AttributionLedger
) -> list[BaselineChange]:
    """The operator acknowledges the report's unattributed holdings as the new baseline.
    Refused (``ValueError``, nothing written) while any order is unresolved: its fill may be
    exactly what is unattributed."""
    if report.unresolved:
        raise ValueError(f"{len(report.unresolved)} order(s) unresolved; settle them first")
    return accept(report.positions, attribution)


def summary_lines(report: AccountReconcileReport) -> list[str]:
    """A human-readable report for the reconcile command (no account identifiers)."""
    counts = {s: sum(1 for o in report.orders if o.outcome is s) for s in Settlement}
    lines = [
        f"orders: {len(report.orders)} open -> completed {counts[Settlement.COMPLETED]}, "
        f"not placed {counts[Settlement.NOT_PLACED]}, unresolved {counts[Settlement.UNRESOLVED]}"
    ]
    lines += [f"  {o.client_order_id}: unresolved [{o.code}] {o.detail}" for o in report.unresolved]
    discrepancies = report.positions.discrepancies
    standing = report.positions.standing
    if standing:
        lines.append(
            f"positions: {len(standing)} acknowledged unattributed holding(s), unchanged: "
            + ", ".join(f"{d.symbol} {d.baseline_qty}" for d in standing)
        )
    if discrepancies:
        lines.append(
            f"positions: {len(discrepancies)} unexplained change(s) against the acknowledged "
            "baseline (your own trades? review, then: trader reconcile --accept-positions)"
        )
        lines += [
            f"  {d.symbol}: broker {d.broker_qty}, attributed {d.attributed_qty}, "
            f"unattributed {d.unattributed_qty}, acknowledged {d.baseline_qty}"
            for d in discrepancies
        ]
    elif not standing:
        lines.append("positions: clean")
    if report.is_clean:
        verdict = "CLEAN"
    elif report.retry_later:  # an unresolved order's fill may explain a position change
        verdict = "NOT CLEAN - run again after the consistency window"
    else:
        verdict = "NOT CLEAN - needs attention"
    lines.append(f"result: {verdict}")
    return lines


__all__ = [
    "AccountReconcileReport",
    "OrderSettlement",
    "Settlement",
    "accept_positions",
    "reconcile_account",
    "summary_lines",
]
