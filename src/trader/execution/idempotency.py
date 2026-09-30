"""Idempotent, crash-safe order placement (design §8.6/§10).

The highest-severity correctness concern in the whole system: a naive retry of an order
whose outcome is unknown (timeout / lost response / crash mid-submit) can place a SECOND
real order and double a real position. This layer guarantees **at-most-once** placement:

1. **One send per client_order_id, ever.** Only the caller whose write-ahead INSERT created
   the ``orders`` row calls ``Broker.submit_order`` for it (the transport may itself re-POST
   a request the server rejected with a 401 — never one it may have processed). Any later
   call for the same id NEVER sends: it *resolves* the row (below). Re-trying an intent means
   a new ``client_order_id`` — the orchestrator makes a fresh decision (and id) every slot.
2. **Durable write-ahead.** The row is committed as ``pending`` BEFORE the network call
   (refused inside an open transaction, where the row could still be rolled back).
3. **Capture the broker id at submit.** The id the broker returns (Schwab: the 201
   ``Location`` header) is recorded immediately, before any status poll. Failing to record
   it is itself an unknown outcome (logged with the id). A successful placement always wins:
   it overwrites a premature ``not_placed``; so does an unknown outcome (a possibly-live order
   must never stay recorded as not placed).
4. **Classify the outcome.** ``OrderNotPlacedError`` from the broker → terminal
   ``not_placed``. Anything else — including a missing id — → ``unknown`` (stamped AFTER the
   send returned) and ``OrderOutcomeUnknownError``.
5. **Resolve, never re-send.** ``resolve`` settles a row without a broker id using an
   injected reconciler: FOUND → adopt the broker's id; ABSENT → ``not_placed``, but only for
   an ``unknown`` row; INCONCLUSIVE (or a raising reconciler) → unresolved. A ``pending`` row
   means the sender died mid-send at an unknown moment (its ``updated_at`` predates the
   send), so ABSENT is not trusted for it: it is re-anchored to ``unknown`` with
   ``updated_at = now`` (after the crash) and resolved on a later pass. Every state change
   bumps the row's ``version`` and resolution is a compare-and-swap on it; a broker id can
   belong to only one row.

**Single-sender precondition.** ``resolve`` treats a ``pending`` row as orphaned, so it must
never run while another process may be sending orders from the same database — e.g. an
operator's ``trader reconcile`` during a slow POST could otherwise re-anchor a live send and
later mark it ``not_placed``. Callers enforce this with the trading lease (the daemon holds
it for its lifetime; the reconcile command refuses while it is held).

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
# values. ``WORKING`` there means "placed; broker id captured" until completion (LR5)
# records the terminal status.
PENDING = "pending"  # write-ahead committed; the one send is in flight or died mid-send
UNKNOWN = "unknown"  # the send returned without a usable answer; stamped after the send
NOT_PLACED = "not_placed"  # terminal: definitely not placed
PLACED = OrderStatus.WORKING.value

_log = get_logger("execution.idempotency")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrderOutcomeUnknownError(Exception):
    """The order may or may not have been placed, and it will not be sent again. Resolve it
    by reconciliation (or a human) before trading this intent again."""

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
    detail: str = ""  # human-readable; must never contain account identifiers
    code: str = ""  # machine-readable reason (e.g. the reconciler's "window_open" = wait)

    def __post_init__(self) -> None:
        has_id = bool((self.broker_order_id or "").strip())
        if (self.outcome is ReconcileOutcome.FOUND) != has_id:
            raise ValueError("broker_order_id is required for FOUND and forbidden otherwise")

    @classmethod
    def found(cls, broker_order_id: str, detail: str = "", code: str = "") -> ReconcileResult:
        return cls(ReconcileOutcome.FOUND, broker_order_id, detail, code)

    @classmethod
    def absent(cls, detail: str = "", code: str = "") -> ReconcileResult:
        return cls(ReconcileOutcome.ABSENT, None, detail, code)

    @classmethod
    def inconclusive(cls, detail: str = "", code: str = "") -> ReconcileResult:
        return cls(ReconcileOutcome.INCONCLUSIVE, None, detail, code)


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
    version: int = 0  # bumped by every state write (compare-and-swap token)

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


# Looks up whether an order for this intent exists at the broker. Contract — ABSENT marks an
# order not_placed, so a false ABSENT leaves a real order untracked. An implementation must:
# - list the broker's orders (every status) over [record.created_at - clock skew, snapshot],
#   the snapshot being the local time the listing was requested;
# - return FOUND only for a unique match of the intent (symbol, side, quantity, type, limit
#   price) whose broker id is not already bound to another local order, and only when no
#   OTHER unresolved local order has the same intent (else it can't tell whose order it is);
# - return ABSENT only when the listing is complete (not truncated; every unparseable item
#   provably not a match) AND snapshot - record.updated_at >= a consistency window that
#   covers the broker's listing lag, server-side processing after a lost response, and clock
#   skew between us and the broker (a forward jump of the local clock shortens it);
# - otherwise return INCONCLUSIVE.
Reconciler = Callable[[OrderRecord], ReconcileResult]


class ResolveOutcome(StrEnum):
    PLACED = "placed"  # the order is at the broker; broker_order_id is known
    NOT_PLACED = "not_placed"  # definitely not at the broker (terminal)
    UNRESOLVED = "unresolved"  # cannot tell yet; try again later or escalate


@dataclass(frozen=True)
class ResolveResult:
    outcome: ResolveOutcome
    broker_order_id: str | None = None
    detail: str = ""


_COLUMNS = (
    "client_order_id, strategy_id, symbol, side, quantity, order_type, limit_price, tif, "
    "status, broker_order_id, created_at, updated_at, version"
)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _parse_ts(text: str) -> datetime:
    """Parse a stored timestamp; a naive value is taken as UTC (as it is written)."""
    ts = datetime.fromisoformat(text)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class OrderRepository:
    """Durable write-ahead state for orders (the ``orders`` table)."""

    def __init__(self, conn: sqlite3.Connection, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._conn = conn
        self._now = now

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def bound_broker_ids(self) -> set[str]:
        """Every broker order id already bound to a local order."""
        rows = self._conn.execute(
            "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL"
        ).fetchall()
        return {str(r[0]) for r in rows}

    def bound_broker_ids_created_between(self, start: datetime, end: datetime) -> set[str]:
        """Broker ids of local orders written within [start, end] — the broker's listing of
        that span must include them (a coverage self-check for reconciliation)."""
        rows = self._conn.execute(
            "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL "
            "AND julianday(created_at) >= julianday(?) AND julianday(created_at) <= julianday(?)",
            (_iso(start), _iso(end)),
        ).fetchall()
        return {str(r[0]) for r in rows}

    def awaiting_resolution(self) -> list[OrderRecord]:
        """Rows whose placement outcome is not settled: pending/unknown with no broker id."""
        rows = self._conn.execute(
            "SELECT client_order_id FROM orders WHERE broker_order_id IS NULL "
            "AND status IN (?, ?) ORDER BY created_at, rowid",
            (PENDING, UNKNOWN),
        ).fetchall()
        records = (self.get(str(r[0])) for r in rows)
        return [r for r in records if r is not None]

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
            created_at=_parse_ts(row[10]),
            updated_at=_parse_ts(row[11]),
            version=int(row[12]),
        )

    def _write_pending(self, order: Order) -> bool:
        """Persist the order as ``pending``. Returns True only for the call that created the
        row — that caller alone may send the order. Private to ``place_idempotent``: a row
        written any other way can never be sent."""
        ts = _iso(self._now())
        limit = format(order.limit_price, "f") if order.limit_price is not None else None
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO orders ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0)",
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

    # -- after the one send (the sender owns the row) --------------------------------- #

    def record_placed(self, client_order_id: str, broker_order_id: str) -> str | None:
        """Record the id of an order the broker accepted. Returns the row's previous status,
        or None if the row is bound to a DIFFERENT broker id. Already bound to this same id
        (e.g. adopted by a resolver first) is success. Overwrites ``not_placed`` — a
        successful placement is ground truth. Raises ``sqlite3.IntegrityError`` if the id is
        already bound to another order."""
        row = self._conn.execute(
            "SELECT status, broker_order_id FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        if row[1] is not None:
            return str(row[0]) if row[1] == broker_order_id else None
        cur = self._conn.execute(
            "UPDATE orders SET status = ?, broker_order_id = ?, updated_at = ?, "
            "version = version + 1 WHERE client_order_id = ? AND broker_order_id IS NULL",
            (PLACED, broker_order_id, _iso(self._now()), client_order_id),
        )
        return str(row[0]) if cur.rowcount == 1 else None

    def mark_unknown_after_send(self, client_order_id: str) -> str | None:
        """The send returned without a usable answer: ``unknown``, stamped NOW (after the
        send), which anchors a reconciler's consistency window after any landing. Overrides
        a premature ``not_placed`` (the order may be live). Returns the previous status."""
        row = self._conn.execute(
            "SELECT status FROM orders WHERE client_order_id = ? AND broker_order_id IS NULL",
            (client_order_id,),
        ).fetchone()
        self._conn.execute(
            "UPDATE orders SET status = ?, updated_at = ?, version = version + 1 "
            "WHERE client_order_id = ? AND broker_order_id IS NULL AND status IN (?, ?, ?)",
            (UNKNOWN, _iso(self._now()), client_order_id, PENDING, UNKNOWN, NOT_PLACED),
        )
        return str(row[0]) if row is not None else None

    def mark_not_placed_after_send(self, client_order_id: str) -> None:
        self._conn.execute(
            "UPDATE orders SET status = ?, updated_at = ?, version = version + 1 "
            "WHERE client_order_id = ? AND broker_order_id IS NULL AND status IN (?, ?)",
            (NOT_PLACED, _iso(self._now()), client_order_id, PENDING, UNKNOWN),
        )

    # -- resolution (compare-and-swap against the record that was read) ---------------- #

    def adopt(self, record: OrderRecord, broker_order_id: str) -> bool:
        """Bind a reconciled broker id to ``record`` iff the row is unchanged since read.
        Raises ``sqlite3.IntegrityError`` if the id is already bound to another order."""
        cur = self._conn.execute(
            "UPDATE orders SET status = ?, broker_order_id = ?, updated_at = ?, "
            "version = version + 1 "
            "WHERE client_order_id = ? AND broker_order_id IS NULL AND version = ?",
            (PLACED, broker_order_id, _iso(self._now()), record.client_order_id, record.version),
        )
        return cur.rowcount == 1

    def transition(self, record: OrderRecord, to_status: str) -> bool:
        """Move ``record`` to ``to_status`` (stamping updated_at) iff unchanged since read."""
        cur = self._conn.execute(
            "UPDATE orders SET status = ?, updated_at = ?, version = version + 1 "
            "WHERE client_order_id = ? AND broker_order_id IS NULL AND version = ?",
            (to_status, _iso(self._now()), record.client_order_id, record.version),
        )
        return cur.rowcount == 1


def _safe_reconcile(reconcile: Reconciler, record: OrderRecord) -> ReconcileResult:
    """A reconciler that raises has not proven anything: treat it as INCONCLUSIVE."""
    try:
        return reconcile(record)
    except Exception as exc:
        # Logged with its traceback: a local bug must not hide behind "inconclusive" forever.
        _log.error(
            "reconciler raised", cid=record.client_order_id, error=type(exc).__name__, exc_info=True
        )
        return ReconcileResult.inconclusive(f"reconciler raised {type(exc).__name__}", "error")


def resolve(repo: OrderRepository, record: OrderRecord, *, reconcile: Reconciler) -> ResolveResult:
    """Settle an order row without ever sending anything (used by retries and by startup
    reconciliation). Precondition: no other process is sending orders from this database
    (see the module docstring's single-sender note)."""
    cid = record.client_order_id
    if record.broker_order_id:
        return ResolveResult(ResolveOutcome.PLACED, record.broker_order_id)
    if record.status == NOT_PLACED:
        return ResolveResult(ResolveOutcome.NOT_PLACED)
    if record.status not in (PENDING, UNKNOWN):
        return ResolveResult(
            ResolveOutcome.UNRESOLVED, detail=f"status {record.status!r} without a broker id"
        )

    result = _safe_reconcile(reconcile, record)
    if result.outcome is ReconcileOutcome.FOUND and result.broker_order_id:
        try:
            adopted = repo.adopt(record, result.broker_order_id)
        except sqlite3.IntegrityError:
            _log.error("reconciled broker id already belongs to another order", cid=cid)
            return ResolveResult(
                ResolveOutcome.UNRESOLVED, detail="broker id already bound to another order"
            )
        if not adopted:
            return ResolveResult(ResolveOutcome.UNRESOLVED, detail="row changed concurrently")
        _log.info("adopted already-placed order", cid=cid, broker_order_id=result.broker_order_id)
        return ResolveResult(ResolveOutcome.PLACED, result.broker_order_id)

    if result.outcome is ReconcileOutcome.ABSENT and record.status == UNKNOWN:
        if not repo.transition(record, NOT_PLACED):
            return ResolveResult(ResolveOutcome.UNRESOLVED, detail="row changed concurrently")
        _log.warning("order confirmed absent at the broker", cid=cid, detail=result.detail)
        return ResolveResult(ResolveOutcome.NOT_PLACED)

    if record.status == PENDING:
        # The sender died mid-send at an unknown moment (updated_at predates the send), so
        # "absent" can't be trusted yet. Re-anchor at now — after the crash — so the
        # consistency window starts after any send the dead process could have made.
        repo.transition(record, UNKNOWN)
        detail = "pending row re-anchored after an interrupted send"
    else:  # an unknown row keeps its anchor: a refusal must not push the window forward
        detail = f"reconcile {result.outcome.value}: {result.detail}"
    return ResolveResult(ResolveOutcome.UNRESOLVED, detail=detail)


def _best_effort(action: Callable[[str], object], cid: str) -> object:
    try:
        return action(cid)
    except Exception as exc:  # the classification below still reaches the caller
        _log.error("could not record order outcome", cid=cid, error=type(exc).__name__)
        return None


def _send_once(broker: Broker, repo: OrderRepository, order: Order) -> str:
    cid = order.client_order_id
    try:
        broker_order_id = broker.submit_order(order)
    except OrderNotPlacedError:
        _best_effort(repo.mark_not_placed_after_send, cid)
        _log.warning("order definitely not placed", cid=cid)
        raise
    except Exception as exc:
        previous = _best_effort(repo.mark_unknown_after_send, cid)
        if previous == NOT_PLACED:
            _log.error("a possibly-live order had been marked not placed; now unknown", cid=cid)
        _log.error("submit outcome unknown; not re-sent", cid=cid, error=type(exc).__name__)
        raise OrderOutcomeUnknownError(cid, type(exc).__name__) from exc
    if not broker_order_id or not broker_order_id.strip():
        _best_effort(repo.mark_unknown_after_send, cid)
        raise OrderOutcomeUnknownError(cid, "broker returned no order id")
    try:
        previous = repo.record_placed(cid, broker_order_id)
    except Exception as exc:
        _log.error(
            "order placed but its broker id could not be recorded",
            cid=cid,
            broker_order_id=broker_order_id,
            error=type(exc).__name__,
        )
        raise OrderOutcomeUnknownError(
            cid, f"placed as {broker_order_id} but not recorded"
        ) from exc
    if previous is None:
        _log.error(
            "order placed but its row is bound to another broker id",
            cid=cid,
            broker_order_id=broker_order_id,
        )
        raise OrderOutcomeUnknownError(cid, f"placed as {broker_order_id}; row bound elsewhere")
    if previous == NOT_PLACED:
        _log.error(
            "order was marked not placed but the broker accepted it; the reconcile window is "
            "too short",
            cid=cid,
            broker_order_id=broker_order_id,
        )
    return broker_order_id


def place_idempotent(
    broker: Broker,
    repo: OrderRepository,
    order: Order,
    *,
    reconcile: Reconciler,
) -> str:
    """Place ``order`` at most once and return its broker order id. Safe to call again with
    the same ``client_order_id`` (retry / crash recovery): a repeat call never sends, it
    resolves the existing row.

    Raises ``OrderNotPlacedError`` (definitely not placed) or ``OrderOutcomeUnknownError``
    (may have been placed; never re-sent)."""
    cid = order.client_order_id
    if repo.in_transaction:
        raise RuntimeError(
            "refusing to send an order inside an open transaction: the write-ahead row must "
            "be committed before the network call"
        )
    if repo._write_pending(order):  # this call created the row: it owns the one send
        return _send_once(broker, repo, order)
    record = repo.get(cid)
    if record is None:  # pragma: no cover - the INSERT OR IGNORE just saw the row
        raise RuntimeError(f"order {cid} vanished between write-ahead and read")
    if record.to_order() != order:
        raise ValueError(f"client_order_id {cid} was already used for a different order")
    result = resolve(repo, record, reconcile=reconcile)
    if result.outcome is ResolveOutcome.PLACED and result.broker_order_id:
        return result.broker_order_id
    if result.outcome is ResolveOutcome.NOT_PLACED:
        raise OrderNotPlacedError(f"order {cid} was not placed")
    raise OrderOutcomeUnknownError(cid, result.detail)


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
    "PLACED",
    "UNKNOWN",
    "OrderOutcomeUnknownError",
    "OrderRecord",
    "OrderRepository",
    "ReconcileOutcome",
    "ReconcileResult",
    "Reconciler",
    "ResolveOutcome",
    "ResolveResult",
    "place_idempotent",
    "resolve",
    "submit_idempotent",
]
