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

Anything that goes wrong after the order is handed to ``place_idempotent`` — other than a
definite ``OrderNotPlacedError`` — calls the ``on_uncertain`` hook (the daemon engages the
kill switch). If the hook itself fails, the executor refuses every later order
(``ExecutionHaltedError``) until the process is restarted.

Backtests do not use this: the orchestrator's direct path (submit + one read against the
synchronous SimBroker) keeps golden runs unchanged.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import Protocol

from trader.core import Fill, Order, OrderNotPlacedError
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

_COMPLETE_ATTEMPTS = 3
_COMPLETE_RETRY_SECONDS = 1.0


class OrderUnresolvedError(Exception):
    """The order was placed (its broker id is recorded) but did not reach a terminal status
    — or its status could not be read. It is left for reconciliation; nothing was
    attributed."""

    def __init__(
        self,
        client_order_id: str,
        broker_order_id: str,
        detail: str,
        last_fill: Fill | None,
        *,
        cancel_attempted: bool = False,
        cancel_accepted: bool = False,
    ) -> None:
        super().__init__(f"order {client_order_id} ({broker_order_id}) unresolved: {detail}")
        self.client_order_id = client_order_id
        self.broker_order_id = broker_order_id
        self.last_fill = last_fill
        # Whether a cancel of the remainder went out — an unaccepted cancel means the order
        # may still be resting at the broker.
        self.cancel_attempted = cancel_attempted
        self.cancel_accepted = cancel_accepted


class ExecutionHaltedError(RuntimeError):
    """An order's fate became uncertain and the kill switch could not be engaged, so this
    executor refuses every further order until the process is restarted (after an operator
    has reconciled, and engaged or released the kill switch by hand)."""


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
        on_uncertain: Callable[[str], object] | None = None,
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
        # Called with a reason when an order's fate is uncertain (the daemon engages the kill
        # switch): no further order may be sent until a human has reconciled it.
        self._on_uncertain = on_uncertain
        self._halted: str | None = None  # set when the hook itself failed

    def execute(self, order: Order) -> Fill:
        if self._halted is not None:
            raise ExecutionHaltedError(self._halted)
        try:
            return self._execute(order)
        except OrderNotPlacedError:
            raise  # definitely not at the broker: nothing is uncertain
        except Exception as exc:
            # Unknown/unresolved outcomes, and anything unexpected in the order path (e.g. a
            # non-retryable error while polling a placed order): halt until reconciled.
            self._uncertain(order, exc)
            raise

    def _uncertain(self, order: Order, exc: Exception) -> None:
        if self._on_uncertain is None:
            return
        reason = f"{type(exc).__name__}: {exc}"
        try:
            self._on_uncertain(reason)
        except Exception as hook_exc:
            # The kill switch could not be engaged: latch this executor instead, so this
            # process sends nothing more until an operator has reconciled and restarted it.
            self._halted = (
                f"order execution halted: could not engage the kill switch "
                f"({type(hook_exc).__name__}) after order {order.client_order_id} failed: {reason}"
            )
            _log.error("could not engage the kill switch; order execution halted", reason=reason)
            raise ExecutionHaltedError(self._halted) from exc

    def _execute(self, order: Order) -> Fill:
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
            raise OrderUnresolvedError(
                cid,
                broker_order_id,
                str(exc),
                exc.last_fill,
                cancel_attempted=exc.cancel_attempted,
                cancel_accepted=exc.cancel_accepted,
            ) from exc
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
                cid,
                broker_order_id,
                f"still {fill.status.value} after polling",
                fill,
                cancel_attempted=result.cancel_attempted,
                cancel_accepted=result.cancel_accepted,
            )
        self._complete(order, broker_order_id, fill)
        return fill

    def _complete(self, order: Order, broker_order_id: str, fill: Fill) -> None:
        """Record the terminal fill atomically. Completion is idempotent, so a transient
        database failure (e.g. a busy lock) is retried a few times; if it still can't be
        recorded — or the fill contradicts the order — the order is unresolved (raised, so
        the uncertainty hook fires), never silently dropped."""
        cid = order.client_order_id
        record = self._repo.get(cid)
        if record is None:  # pragma: no cover - place_idempotent just wrote it
            raise RuntimeError(f"order {cid} has no row")
        apply = partial(self._attribution.apply, fill, order.strategy_id, order.side)
        last_error: Exception | None = None
        for attempt in range(_COMPLETE_ATTEMPTS):
            try:
                self._repo.complete(record, fill, apply)
                return
            except sqlite3.OperationalError as exc:  # busy/locked or I/O: worth a retry
                last_error = exc
                _log.warning(
                    "could not record a terminal fill; retrying",
                    cid=cid,
                    broker_order_id=broker_order_id,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                self._sleep(_COMPLETE_RETRY_SECONDS)
            except ValueError as exc:  # the fill contradicts the order: never attribute it
                raise OrderUnresolvedError(
                    cid, broker_order_id, f"fill rejected: {exc}", fill
                ) from exc
        raise OrderUnresolvedError(
            cid,
            broker_order_id,
            f"{fill.status.value} {fill.quantity} filled but not recorded "
            f"({type(last_error).__name__})",
            fill,
        ) from last_error


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
    "ExecutionHaltedError",
    "OrderExecutor",
    "OrderUnresolvedError",
    "in_memory_reconciler",
]
