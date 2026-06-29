"""Idempotent, crash-safe order placement (design §8.6/§10).

The highest-severity correctness concern in the whole system: a naive retry of an order
whose outcome is unknown (timeout / lost response / crash mid-submit) can place a SECOND
real order and double a real position. This layer guarantees **at-most-once** placement:

1. **Write-ahead.** The order is persisted as ``pending`` (keyed by ``client_order_id``)
   BEFORE the network call, so the intent is durable even if the process dies mid-submit.
2. **Reconcile-before-(re)send.** Whenever we are about to send an order for which we hold
   no broker order id, we first ask the broker whether an order for this intent already
   landed (a prior attempt may have succeeded but lost its response). If it did, we adopt it
   and poll — we never resend.
3. **Reuse the same id.** Retries reuse the same ``client_order_id``; once a broker order id
   is known we only ever poll it, never resubmit.

The transport also refuses to auto-retry the order POST (M5.1), so a duplicate can't be
created beneath this layer either. The ``reconcile`` hook is injected: in tests it queries
the FakeBroker; in production it queries Schwab's open/recent orders for a matching intent
(authoritative reconciliation is the [VERIFY] precondition for safe resends).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from trader.core import Fill, Order
from trader.core.protocols import Broker
from trader.observability.logging import get_logger

# Finds an already-landed order for this intent (else None). Authoritative: a None means
# "confirmed not present", which is what makes a resend safe.
Reconciler = Callable[[Order], Fill | None]

_PENDING = "pending"

_log = get_logger("execution.idempotency")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OrderRecord:
    client_order_id: str
    status: str
    broker_order_id: str | None


class OrderRepository:
    """Durable write-ahead state for orders (the ``orders`` table)."""

    def __init__(self, conn: sqlite3.Connection, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._conn = conn
        self._now = now

    def get(self, client_order_id: str) -> OrderRecord | None:
        row = self._conn.execute(
            "SELECT client_order_id, status, broker_order_id FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return OrderRecord(row[0], row[1], row[2])

    def write_pending(self, order: Order) -> bool:
        """Persist the order as ``pending`` BEFORE submit. Returns False if it already
        existed (a retry) — never overwrites an in-flight/known order."""
        ts = self._now().astimezone(UTC).isoformat()
        limit = format(order.limit_price, "f") if order.limit_price is not None else None
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO orders (client_order_id, strategy_id, symbol, side, quantity, "
            "order_type, limit_price, tif, status, broker_order_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                order.client_order_id,
                order.strategy_id,
                order.symbol,
                order.side.value,
                order.quantity,
                order.order_type.value,
                limit,
                order.tif.value,
                _PENDING,
                ts,
                ts,
            ),
        )
        return cur.rowcount > 0

    def mark_submitted(self, client_order_id: str, broker_order_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE orders SET status = ?, broker_order_id = ?, updated_at = ? "
            "WHERE client_order_id = ?",
            (status, broker_order_id, self._now().astimezone(UTC).isoformat(), client_order_id),
        )


def submit_idempotent(
    broker: Broker,
    repo: OrderRepository,
    order: Order,
    *,
    reconcile: Reconciler,
) -> Fill:
    """Place ``order`` at most once. Safe to call repeatedly (retry/crash recovery) with the
    same ``client_order_id`` — it never produces a duplicate broker order."""
    record = repo.get(order.client_order_id)

    # Already submitted (this process or a prior crash): only ever poll, NEVER resubmit.
    if record is not None and record.broker_order_id:
        return broker.get_order(record.broker_order_id)

    if record is None:
        repo.write_pending(order)  # WRITE-AHEAD: durable intent before any network call

    # We hold no broker order id. A prior attempt may have landed but lost its response, so
    # reconcile BEFORE sending — adopt an existing order rather than risk a duplicate.
    existing = reconcile(order)
    if existing is not None:
        _log.info(
            "adopted already-landed order (reconcile-before-resend)",
            cid=order.client_order_id,
            broker_order_id=existing.broker_order_id,
        )
        repo.mark_submitted(order.client_order_id, existing.broker_order_id, existing.status.value)
        return broker.get_order(existing.broker_order_id)

    # No evidence it landed -> safe to submit, reusing the same client_order_id.
    try:
        broker_order_id = broker.submit_order(order)
    except Exception:
        # Unknown outcome: leave the row 'pending'. Do NOT resend here — the next call
        # reconciles first (above), so an order of unknown fate is never blindly re-sent.
        _log.warning("submit failed with unknown outcome; left pending", cid=order.client_order_id)
        raise
    fill = broker.get_order(broker_order_id)
    repo.mark_submitted(order.client_order_id, broker_order_id, fill.status.value)
    return fill


__all__ = ["OrderRecord", "OrderRepository", "Reconciler", "submit_idempotent"]
