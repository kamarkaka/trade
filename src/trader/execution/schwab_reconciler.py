"""Production order reconciler over Schwab's order listing (LR4; design §8.6/§10).

Answers the question the placement layer cannot: did an order whose outcome is unknown
actually land? Implements the ``Reconciler`` contract in ``execution.idempotency``. A false
ABSENT leaves a live order recorded ``not_placed``; a false FOUND attributes someone else's
order (e.g. a manual trade) to a strategy. Schwab does not echo our ``client_order_id``, so
the answer rests on the intent AND on when the order was entered:

- Only an ``unknown`` row is reconciled. Its send happened between the write-ahead
  (``created_at``) and the unknown mark (``updated_at``, stamped after the send returned),
  so Schwab's ``enteredTime`` for OUR order lies in that **send window** (± clock skew). An
  identical order entered at any other time is not ours. (A ``pending`` row — sender died
  mid-send — has no upper bound yet: INCONCLUSIVE; ``resolve`` re-anchors it.)
- The listing is requested with a wide margin (± a day, within Schwab's look-back limit) and
  filtered here on each order's own ``enteredTime`` — never trusting the server's reading of
  the time bounds. It must include every local order already bound to a broker id inside the
  send window, or it is treated as incomplete.
- Both answers wait out the **consistency window** since ``updated_at``: until then our
  order may simply not be visible, so neither "it's that one" nor "there's none" is safe.
- **FOUND** only if exactly one unbound listed order could be ours, it is an exact match
  (canonical symbol, the exact instruction we send, quantity, type, LIMIT price, entered in
  the send window), and no other unresolved local order with the same intent could own it
  (its own send window also contains that entry time).
- **ABSENT** only if no unbound listed order could be ours and no unparseable one might be.
- Otherwise **INCONCLUSIVE**, with a machine-readable ``code`` (``WINDOW_OPEN`` means "try
  again later"; the rest need a human or a later listing).

"Could be ours" is deliberately loose — every field the listing reports must merely be
compatible (unknown/empty fields, a short-sale spelling of our side, a price within a tick,
a top-level or leg quantity equal to ours, ``BRK.B`` vs ``BRK/B``) — so doubt blocks ABSENT
instead of producing it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from decimal import Decimal

from trader.core.enums import OrderType
from trader.core.protocols import Clock
from trader.execution.idempotency import UNKNOWN, OrderRecord, ReconcileResult
from trader.observability.logging import get_logger
from trader.schwab.orders import SchwabOrderStatus, SchwabTradingClient

# Reason codes on INCONCLUSIVE results.
WINDOW_OPEN = "window_open"  # our order may not be visible yet — retry after the window
NOT_SETTLED = "not_settled"  # not an 'unknown' row (e.g. pending: resolve re-anchors it)
AMBIGUOUS = "ambiguous"  # a listed order might be ours but can't be proven to be
LISTING_FAILED = "listing_failed"  # the listing errored or may be truncated
LISTING_INCOMPLETE = "listing_incomplete"  # an order we know of is missing from the listing
TOO_OLD = "too_old"  # beyond the broker's listing look-back: resolve manually

LOOKBACK_LIMIT = timedelta(days=59)  # Schwab lists at most ~60 days back [VERIFY]
LISTING_MARGIN = timedelta(days=1)  # server-side bounds are only a coarse prefilter
PRICE_TICK = Decimal("0.01")  # a LIMIT price within a tick may be the broker's rounding

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
        bound_broker_ids_created_between: Callable[[datetime, datetime], Collection[str]],
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
        self._bound_created_between = bound_broker_ids_created_between
        self._awaiting_resolution = awaiting_resolution
        self._window = consistency_window
        self._skew = clock_skew

    def __call__(self, record: OrderRecord) -> ReconcileResult:
        if record.status != UNKNOWN:
            return ReconcileResult.inconclusive(
                f"status {record.status!r} has no send window yet", NOT_SETTLED
            )
        snapshot = self._clock.now()  # the local time the listing is requested
        send_lo, send_hi = record.created_at - self._skew, record.updated_at + self._skew
        if snapshot - send_lo > LOOKBACK_LIMIT:
            return ReconcileResult.inconclusive(
                "the order is older than the broker's listing look-back", TOO_OLD
            )
        try:
            listing = self._client.get_orders(
                self._account,
                from_entered=max(send_lo - LISTING_MARGIN, snapshot - LOOKBACK_LIMIT),
                to_entered=snapshot + LISTING_MARGIN,
            )
        except Exception as exc:  # a failed or possibly-truncated listing proves nothing
            return ReconcileResult.inconclusive(
                f"order listing failed ({type(exc).__name__})", LISTING_FAILED
            )

        listed = {o.order_id for o in listing.orders} | {u.order_id for u in listing.unparsed}
        missing = set(self._bound_created_between(send_lo, send_hi)) - listed
        if missing:
            _log.error(
                "order listing is missing orders we placed; not trusting it",
                cid=record.client_order_id,
                missing=len(missing),
            )
            return ReconcileResult.inconclusive(
                f"{len(missing)} known order(s) missing from the listing", LISTING_INCOMPLETE
            )
        waited = snapshot - record.updated_at
        if waited < self._window:
            return ReconcileResult.inconclusive(
                f"inside the consistency window ({waited.total_seconds():.0f}s elapsed)",
                WINDOW_OPEN,
            )

        bound = set(self._bound_broker_ids())
        candidates = [
            o
            for o in listing.orders
            if o.order_id not in bound
            and _could_be(o, record)
            and _entered_within(o, send_lo, send_hi)
        ]
        if any(
            u.order_id not in bound and _canon(u.symbol) in ("", _canon(record.symbol))
            for u in listing.unparsed
        ):
            return ReconcileResult.inconclusive(
                "an unparseable listed order may be this one", AMBIGUOUS
            )
        if not candidates:
            return ReconcileResult.absent("no listed order could be this one")
        if len(candidates) > 1:
            return ReconcileResult.inconclusive(
                f"{len(candidates)} listed orders may be this one", AMBIGUOUS
            )
        (only,) = candidates
        if not _exact_match(only, record, send_lo, send_hi):
            return ReconcileResult.inconclusive("a similar listed order may be this one", AMBIGUOUS)
        for rival in self._awaiting_resolution():
            if rival.client_order_id == record.client_order_id or not _same_intent(rival, record):
                continue
            rival_hi = rival.updated_at if rival.status == UNKNOWN else snapshot
            if _entered_within(only, rival.created_at - self._skew, rival_hi + self._skew):
                return ReconcileResult.inconclusive(
                    "another unresolved local order may own the matching order", AMBIGUOUS
                )
        return ReconcileResult.found(only.order_id, "unique intent match in the send window")


def _canon(symbol: str) -> str:
    """Comparable form of a ticker: trimmed, upper-case, share-class separator unified."""
    return symbol.strip().upper().replace("/", ".")


def _entered_within(o: SchwabOrderStatus, lo: datetime, hi: datetime) -> bool:
    """Entered inside [lo, hi] — or of unknown entry time (which can't be ruled out)."""
    return o.entered_time is None or lo <= o.entered_time <= hi


def _same_intent(a: OrderRecord, b: OrderRecord) -> bool:
    return (
        _canon(a.symbol) == _canon(b.symbol)
        and a.side is b.side
        and a.quantity == b.quantity
        and a.order_type is b.order_type
        and a.limit_price == b.limit_price
    )


def _exact_match(o: SchwabOrderStatus, record: OrderRecord, lo: datetime, hi: datetime) -> bool:
    """Every field known and equal to the intent, entered inside the send window."""
    return (
        _canon(o.symbol) == _canon(record.symbol)
        and o.instruction == record.side.value
        and o.quantity == record.quantity
        and o.leg_quantity in (0, record.quantity)
        and o.order_type == record.order_type.value
        and (record.order_type is not OrderType.LIMIT or o.price == record.limit_price)
        and o.entered_time is not None
        and lo <= o.entered_time <= hi
    )


def _could_be(o: SchwabOrderStatus, record: OrderRecord) -> bool:
    """Every field the listing reports is compatible with the intent (an empty/None field
    is unknown, never "different") — so this order can't be ruled out."""
    quantity_ok = o.quantity in (0, record.quantity) or o.leg_quantity == record.quantity
    price_ok = (
        record.order_type is not OrderType.LIMIT
        or o.price is None
        or record.limit_price is None
        or abs(o.price - record.limit_price) <= PRICE_TICK
    )
    return (
        _canon(o.symbol) in ("", _canon(record.symbol))
        and o.side in (None, record.side)
        and quantity_ok
        and o.order_type in ("", record.order_type.value)
        and price_ok
    )


__all__ = [
    "AMBIGUOUS",
    "LISTING_FAILED",
    "LISTING_INCOMPLETE",
    "NOT_SETTLED",
    "TOO_OLD",
    "WINDOW_OPEN",
    "SchwabOrderReconciler",
]
