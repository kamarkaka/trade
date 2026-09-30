"""Production order reconciler over Schwab's order listing (LR4; design §8.6/§10).

Answers the question the placement layer cannot: did an order whose outcome is unknown
actually land? Implements the ``Reconciler`` contract in ``execution.idempotency`` — a
false ABSENT marks a live order ``not_placed``, so absence must be proven:

- the account's orders are listed from ``record.created_at - clock_skew`` to the listing
  snapshot (+ skew);
- FOUND only for a UNIQUE exact match of the intent — same symbol, the exact instruction we
  send (BUY/SELL), quantity, order type and LIMIT price, entered inside the window — whose
  broker id is not already bound to another local order, and only when no OTHER unresolved
  local order shares the intent (a single listed order could then be either one's);
- INCONCLUSIVE whenever absence cannot be proven: the listing failed or may be truncated;
  several orders match exactly; a listed order COULD be this one (every field it reports is
  compatible — e.g. a short-sale instruction on the same side, or missing fields); an
  unparseable listed order on this symbol (or of unknown symbol); or, at the snapshot,
  the consistency window since ``record.updated_at`` (stamped at/after the last send) has
  not elapsed;
- ABSENT only when none of the above holds.

The listing covers every status (filled, cancelled, rejected orders count as "landed"); the
snapshot is the local time the listing was requested, so a slow, retried request only makes
the window check more conservative — but a forward jump of the local clock shortens it.

Schwab does not echo our ``client_order_id``, hence the intent matching. It is only needed
for orders whose broker id was never captured — normally the id is recorded at submit (LR3).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta

from trader.core.enums import OrderType
from trader.core.protocols import Clock
from trader.execution.idempotency import OrderRecord, ReconcileResult
from trader.observability.logging import get_logger
from trader.schwab.orders import SchwabOrderStatus, SchwabTradingClient

_log = get_logger("execution.schwab_reconciler")


class SchwabOrderReconciler:
    """``Reconciler`` over ``SchwabTradingClient.get_orders`` (read-only)."""

    def __init__(
        self,
        client: SchwabTradingClient,
        account_hash: str,
        *,
        clock: Clock,
        bound_broker_ids: Callable[[], Collection[str]],
        awaiting_resolution: Callable[[], Collection[OrderRecord]],
        consistency_window: timedelta = timedelta(minutes=5),
        clock_skew: timedelta = timedelta(minutes=2),
    ) -> None:
        if consistency_window <= timedelta(0):
            raise ValueError("consistency_window must be positive")
        if clock_skew < timedelta(0):
            raise ValueError("clock_skew must be non-negative")
        self._client = client
        self._account = account_hash
        self._clock = clock
        self._bound_broker_ids = bound_broker_ids
        self._awaiting_resolution = awaiting_resolution
        self._window = consistency_window
        self._skew = clock_skew

    def __call__(self, record: OrderRecord) -> ReconcileResult:
        snapshot = self._clock.now()
        start = record.created_at - self._skew
        end = snapshot + self._skew
        try:
            listing = self._client.get_orders(self._account, from_entered=start, to_entered=end)
            bound = set(self._bound_broker_ids())
            rivals = [
                r
                for r in self._awaiting_resolution()
                if r.client_order_id != record.client_order_id and _same_intent(r, record)
            ]
        except Exception as exc:  # a failed or possibly-truncated listing proves nothing
            return ReconcileResult.inconclusive(f"order listing failed ({type(exc).__name__})")

        candidates = [o for o in listing.orders if o.order_id not in bound]
        exact = [o for o in candidates if _exact_match(o, record, start, end)]
        if exact and rivals:
            return ReconcileResult.inconclusive(
                f"{len(rivals)} other unresolved local order(s) share this intent"
            )
        if len(exact) == 1:
            return ReconcileResult.found(exact[0].order_id, "unique intent match")
        if exact:
            _log.error(
                "several listed orders match one unknown-outcome intent",
                cid=record.client_order_id,
                matches=len(exact),
            )
            return ReconcileResult.inconclusive(f"{len(exact)} listed orders match this intent")
        if any(_could_be(o, record) for o in candidates):
            return ReconcileResult.inconclusive("a similar listed order may be this one")
        if any(u.symbol in ("", record.symbol) for u in listing.unparsed):
            return ReconcileResult.inconclusive("an unparseable listed order may be this one")
        waited = snapshot - record.updated_at
        if waited < self._window:
            return ReconcileResult.inconclusive(
                f"inside the consistency window ({waited.total_seconds():.0f}s elapsed)"
            )
        return ReconcileResult.absent("no matching order listed after the consistency window")


def _exact_match(o: SchwabOrderStatus, record: OrderRecord, start: datetime, end: datetime) -> bool:
    """Every field known and equal to the intent, entered inside the listing window."""
    return (
        o.symbol == record.symbol
        and o.instruction == record.side.value
        and o.quantity == record.quantity
        and o.order_type == record.order_type.value
        and (record.order_type is not OrderType.LIMIT or o.price == record.limit_price)
        and o.entered_time is not None
        and start <= o.entered_time <= end
    )


def _same_intent(a: OrderRecord, b: OrderRecord) -> bool:
    return (
        a.symbol == b.symbol
        and a.side is b.side
        and a.quantity == b.quantity
        and a.order_type is b.order_type
        and a.limit_price == b.limit_price
    )


def _could_be(o: SchwabOrderStatus, record: OrderRecord) -> bool:
    """Every field the listing reports is compatible with the intent (an empty/None field
    is unknown, never "different") — so this order can't be ruled out."""
    return (
        o.symbol in ("", record.symbol)
        and o.side in (None, record.side)
        and o.quantity in (0, record.quantity)
        and o.order_type in ("", record.order_type.value)
        and (
            record.order_type is not OrderType.LIMIT
            or o.price is None
            or o.price == record.limit_price
        )
    )


__all__ = ["SchwabOrderReconciler"]
