"""Durable order execution for the paper/live daemon (LR5; design §4.2).

``DurableOrderExecutor.execute(order)`` runs one order through the full live sequence:

1. **Place at most once** (``place_idempotent``): durable write-ahead row, one send per
   ``client_order_id``, broker id recorded at submit. A definite rejection raises
   ``OrderNotPlacedError``; an unknown outcome raises ``OrderOutcomeUnknownError`` (never
   re-sent — reconciliation settles it).
2. **Poll to a terminal status** (``poll_until_terminal``), cancelling any remainder at the
   deadline. If the order still isn't terminal — or its status can't be read — it raises
   ``OrderUnresolvedError`` and leaves the row non-terminal (broker id known) for
   reconciliation: shares are never attributed from a state we can't trust.
3. **Complete atomically** (``OrderRepository.complete``): terminal status + fill row +
   per-strategy attribution in ONE transaction, exactly once.

Backtests do not use this: the orchestrator's direct path (submit + one read against the
synchronous SimBroker) keeps golden runs unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from trader.core import Fill, Order
from trader.core.protocols import Broker
from trader.execution.idempotency import (
    OrderRecord,
    OrderRepository,
    Reconciler,
    ReconcileResult,
    place_idempotent,
)
from trader.execution.poller import (
    DEFAULT_RETRYABLE,
    OrderStatusUnavailableError,
    PollPolicy,
    poll_until_terminal,
)
from trader.observability.logging import get_logger
from trader.state.attribution import AttributionLedger

_log = get_logger("execution.executor")


class OrderUnresolvedError(Exception):
    """The order was placed (its broker id is recorded) but did not reach a terminal status
    — or its status could not be read. It is left for reconciliation; nothing was
    attributed."""

    def __init__(
        self, client_order_id: str, broker_order_id: str, detail: str, last_fill: Fill | None
    ) -> None:
        super().__init__(f"order {client_order_id} ({broker_order_id}) unresolved: {detail}")
        self.client_order_id = client_order_id
        self.broker_order_id = broker_order_id
        self.last_fill = last_fill


class OrderExecutor(Protocol):
    def execute(self, order: Order) -> Fill: ...


class DurableOrderExecutor:
    """Place → poll → complete, durably (see the module docstring)."""

    def __init__(
        self,
        *,
        broker: Broker,
        repo: OrderRepository,
        attribution: AttributionLedger,
        reconcile: Reconciler,
        poll_policy: PollPolicy,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retryable: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
    ) -> None:
        if attribution.connection is not repo.connection:
            # Completion must update orders, fills and attribution in ONE transaction.
            raise ValueError("attribution and orders must share one database connection")
        self._broker = broker
        self._repo = repo
        self._attribution = attribution
        self._reconcile = reconcile
        self._policy = poll_policy
        self._monotonic = monotonic
        self._sleep = sleep
        self._retryable = retryable

    def execute(self, order: Order) -> Fill:
        cid = order.client_order_id
        broker_order_id = place_idempotent(
            self._broker, self._repo, order, reconcile=self._reconcile
        )
        try:
            result = poll_until_terminal(
                self._broker,
                broker_order_id,
                self._policy,
                monotonic=self._monotonic,
                sleep=self._sleep,
                retryable=self._retryable,
            )
        except OrderStatusUnavailableError as exc:
            raise OrderUnresolvedError(cid, broker_order_id, str(exc), exc.last_fill) from exc
        fill = replace(result.fill, client_order_id=cid)  # the broker may not know our id
        if not result.terminal:
            _log.error(
                "order unresolved after polling",
                cid=cid,
                broker_order_id=broker_order_id,
                status=fill.status.value,
                filled=fill.quantity,
            )
            raise OrderUnresolvedError(
                cid, broker_order_id, f"still {fill.status.value} after polling", fill
            )
        record = self._repo.get(cid)
        if record is None:  # pragma: no cover - place_idempotent just wrote it
            raise RuntimeError(f"order {cid} has no row")
        self._repo.complete(
            record, fill, lambda: self._attribution.apply(fill, order.strategy_id, order.side)
        )
        return fill


def in_memory_reconciler(find_by_client_id: Callable[[str], Fill | None]) -> Reconciler:
    """Reconciler over an in-memory broker's own record of what it received (SimBroker in
    paper, FakeBroker in tests) — authoritative there, so ABSENT is exact."""

    def reconcile(record: OrderRecord) -> ReconcileResult:
        fill = find_by_client_id(record.client_order_id)
        if fill is None:
            return ReconcileResult.absent("not received by the in-memory broker")
        return ReconcileResult.found(fill.broker_order_id)

    return reconcile


__all__ = [
    "DurableOrderExecutor",
    "OrderExecutor",
    "OrderUnresolvedError",
    "in_memory_reconciler",
]
