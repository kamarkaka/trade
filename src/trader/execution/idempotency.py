"""Idempotent, crash-safe order placement (design §8.6/§10).

The highest-severity correctness concern in the whole system: a naive retry of an order
whose outcome is unknown (timeout / lost response / crash mid-submit) can place a SECOND
real order and double a real position. This layer guarantees **at-most-once** placement:

1. **Write-ahead.** The order is persisted as ``pending`` (keyed by ``client_order_id``)
   BEFORE the network call, so the intent is durable even if the process dies mid-submit.
2. **Capture the broker id at submit.** The id the broker returns (Schwab: the 201
   ``Location`` header) is persisted immediately — before any status poll — so a later
   crash or a failing poll can never lose it and force intent-matching.
3. **Classify the outcome.** ``OrderNotPlacedError`` from the broker means definitely not
   placed (terminal ``not_placed``). Any other failure means the outcome is UNKNOWN: the
   row is marked ``unknown`` and ``OrderOutcomeUnknownError`` is raised — never a resend.
4. **Reconcile before any re-send.** A second attempt for a ``client_order_id`` that has a
   row but no broker id first asks the injected reconciler. FOUND → adopt the broker's id
   and never resend; ABSENT (authoritative: the consistency window has elapsed and nothing
   matches) → resend with the same ``client_order_id``; INCONCLUSIVE (lagging, ambiguous,
   or errored lookup) → refuse with ``OrderOutcomeUnknownError`` so a human/reconciliation
   resolves it. A reconciler must therefore return ABSENT only when it is sure — in
   particular its consistency window must be anchored at ``OrderRecord.updated_at`` (which
   is at or after the LAST send attempt), never at ``created_at``: a re-send that lands
   late would otherwise look "absent" and be sent a third time.

The transport also refuses to auto-retry the order POST (M5.1), so a duplicate can't be
created beneath this layer either.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from trader.core import Fill, Order, OrderNotPlacedError
from trader.core.enums import OrderStatus, OrderType, Side, TimeInForce
from trader.core.protocols import Broker
from trader.observability.logging import get_logger

# Local lifecycle states stored in ``orders.status`` alongside the broker's OrderStatus
# values (WORKING/FILLED/...). ``WORKING`` there means "placed; broker id captured".
PENDING = "pending"  # write-ahead done; the send may or may not have happened yet
UNKNOWN = "unknown"  # sent (or maybe sent) with an unknown outcome — never resent blindly
NOT_PLACED = "not_placed"  # definitely not placed (terminal)

_log = get_logger("execution.idempotency")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrderOutcomeUnknownError(Exception):
    """The order may or may not have been placed; it was NOT (re)sent. Resolve it by
    reconciliation (or a human) before trading this intent again."""

    def __init__(self, client_order_id: str, detail: str) -> None:
        super().__init__(f"order {client_order_id}: outcome unknown ({detail}); not re-sent")
        self.client_order_id = client_order_id
        self.detail = detail


class ReconcileOutcome(StrEnum):
    FOUND = "found"  # a unique broker order matches this intent
    ABSENT = "absent"  # authoritative: no such order exists at the broker
    INCONCLUSIVE = "inconclusive"  # cannot tell yet (lagging / ambiguous / lookup failed)


@dataclass(frozen=True)
class ReconcileResult:
    outcome: ReconcileOutcome
    broker_order_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.outcome is ReconcileOutcome.FOUND) != bool(self.broker_order_id):
            raise ValueError("broker_order_id is required for FOUND and forbidden otherwise")

    @classmethod
    def found(cls, broker_order_id: str, detail: str = "") -> ReconcileResult:
        return cls(ReconcileOutcome.FOUND, broker_order_id, detail)

    @classmethod
    def absent(cls, detail: str = "") -> ReconcileResult:
        return cls(ReconcileOutcome.ABSENT, None, detail)

    @classmethod
    def inconclusive(cls, detail: str = "") -> ReconcileResult:
        return cls(ReconcileOutcome.INCONCLUSIVE, None, detail)


@dataclass(frozen=True)
class OrderRecord:
    """One durable ``orders`` row: the full intent plus its lifecycle state."""

    client_order_id: str
    strategy_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None
    tif: TimeInForce
    status: str
    broker_order_id: str | None
    created_at: datetime
    updated_at: datetime

    def to_order(self) -> Order:
        return Order(
            client_order_id=self.client_order_id,
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            tif=self.tif,
        )


# Looks up whether an order for this intent already exists at the broker. Must return
# ABSENT only when it is authoritative (see the module docstring).
Reconciler = Callable[[OrderRecord], ReconcileResult]

_COLUMNS = (
    "client_order_id, strategy_id, symbol, side, quantity, order_type, limit_price, tif, "
    "status, broker_order_id, created_at, updated_at"
)


class OrderRepository:
    """Durable write-ahead state for orders (the ``orders`` table)."""

    def __init__(self, conn: sqlite3.Connection, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._conn = conn
        self._now = now

    def get(self, client_order_id: str) -> OrderRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        if row is None:
            return None
        return OrderRecord(
            client_order_id=row[0],
            strategy_id=row[1],
            symbol=row[2],
            side=Side(row[3]),
            quantity=int(row[4]),
            order_type=OrderType(row[5]),
            limit_price=Decimal(row[6]) if row[6] is not None else None,
            tif=TimeInForce(row[7]),
            status=row[8],
            broker_order_id=row[9],
            created_at=datetime.fromisoformat(row[10]),
            updated_at=datetime.fromisoformat(row[11]),
        )

    def write_pending(self, order: Order) -> bool:
        """Persist the order as ``pending`` BEFORE submit. Returns False if it already
        existed (a retry) — never overwrites an in-flight/known order."""
        ts = self._ts()
        limit = format(order.limit_price, "f") if order.limit_price is not None else None
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO orders ({_COLUMNS}) "
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
                PENDING,
                ts,
                ts,
            ),
        )
        return cur.rowcount > 0

    def mark_placed(self, client_order_id: str, broker_order_id: str) -> None:
        """Record the broker's id for a placed (or adopted) order — status ``WORKING`` until
        its terminal status is recorded."""
        self._set(client_order_id, OrderStatus.WORKING.value, broker_order_id)

    def mark_unknown(self, client_order_id: str) -> None:
        self._set(client_order_id, UNKNOWN)

    def mark_not_placed(self, client_order_id: str) -> None:
        self._set(client_order_id, NOT_PLACED)

    def mark_pending(self, client_order_id: str) -> None:
        self._set(client_order_id, PENDING)

    def _set(self, client_order_id: str, status: str, broker_order_id: str | None = None) -> None:
        if broker_order_id is None:
            self._conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE client_order_id = ?",
                (status, self._ts(), client_order_id),
            )
        else:
            self._conn.execute(
                "UPDATE orders SET status = ?, broker_order_id = ?, updated_at = ? "
                "WHERE client_order_id = ?",
                (status, broker_order_id, self._ts(), client_order_id),
            )

    def _ts(self) -> str:
        return self._now().astimezone(UTC).isoformat()


def _safe_reconcile(reconcile: Reconciler, record: OrderRecord) -> ReconcileResult:
    """A reconciler that raises has not proven anything: treat it as INCONCLUSIVE."""
    try:
        return reconcile(record)
    except Exception as exc:
        return ReconcileResult.inconclusive(f"reconciler raised {type(exc).__name__}")


def place_idempotent(
    broker: Broker,
    repo: OrderRepository,
    order: Order,
    *,
    reconcile: Reconciler,
) -> str:
    """Place ``order`` at most once and return its broker order id. Safe to call again with
    the same ``client_order_id`` (retry / crash recovery): it never produces a duplicate.

    Raises ``OrderNotPlacedError`` (definitely not placed) or ``OrderOutcomeUnknownError``
    (may have been placed; not re-sent)."""
    cid = order.client_order_id
    if not repo.write_pending(order):  # WRITE-AHEAD; False => a prior attempt exists
        record = repo.get(cid)
        if record is None:  # pragma: no cover - the INSERT OR IGNORE just saw the row
            raise RuntimeError(f"order {cid} vanished between write-ahead and read")
        if record.broker_order_id:
            return record.broker_order_id  # already placed: only ever poll, never resubmit
        if record.status != NOT_PLACED:
            # A prior attempt may have landed with its response lost: reconcile BEFORE any
            # re-send and adopt an existing order rather than risk a duplicate.
            result = _safe_reconcile(reconcile, record)
            if result.outcome is ReconcileOutcome.FOUND and result.broker_order_id:
                _log.info(
                    "adopted already-placed order (reconcile-before-resend)",
                    cid=cid,
                    broker_order_id=result.broker_order_id,
                )
                repo.mark_placed(cid, result.broker_order_id)
                return result.broker_order_id
            if result.outcome is not ReconcileOutcome.ABSENT:
                _log.error("refusing re-send: order outcome unknown", cid=cid, detail=result.detail)
                if record.status != UNKNOWN:
                    # Only a state CHANGE touches updated_at: it anchors the reconciler's
                    # consistency window at (or after) the last send, and a refused retry of
                    # an already-unknown row must not push that anchor forward forever.
                    repo.mark_unknown(cid)
                raise OrderOutcomeUnknownError(cid, f"reconcile inconclusive: {result.detail}")
            # ABSENT is authoritative: the earlier attempt never landed -> safe to re-send.
        repo.mark_pending(cid)

    try:
        broker_order_id = broker.submit_order(order)
    except OrderNotPlacedError:
        repo.mark_not_placed(cid)
        _log.warning("order definitely not placed", cid=cid)
        raise
    except Exception as exc:
        # Unknown outcome: the order may be live. Never resend here — a later attempt must
        # reconcile first (above), and only an authoritative ABSENT permits a re-send.
        repo.mark_unknown(cid)
        _log.error("submit outcome unknown; not re-sent", cid=cid, error=type(exc).__name__)
        raise OrderOutcomeUnknownError(cid, type(exc).__name__) from exc
    repo.mark_placed(cid, broker_order_id)  # capture the id BEFORE any status poll
    return broker_order_id


def submit_idempotent(
    broker: Broker,
    repo: OrderRepository,
    order: Order,
    *,
    reconcile: Reconciler,
) -> Fill:
    """``place_idempotent`` followed by a single status read (no polling — see
    ``execution.poller`` for bounded polling to a terminal status)."""
    return broker.get_order(place_idempotent(broker, repo, order, reconcile=reconcile))


__all__ = [
    "NOT_PLACED",
    "PENDING",
    "UNKNOWN",
    "OrderOutcomeUnknownError",
    "OrderRecord",
    "OrderRepository",
    "ReconcileOutcome",
    "ReconcileResult",
    "Reconciler",
    "place_idempotent",
    "submit_idempotent",
]
