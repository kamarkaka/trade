"""Production order reconciler over Schwab's order listing (LR4; design §8.6/§10).

Answers the question the placement layer cannot: did an order whose outcome is unknown
actually land? Implements the ``Reconciler`` contract in ``execution.idempotency``. A false
ABSENT leaves a live order recorded ``not_placed``; a false FOUND attributes someone else's
order (e.g. a manual trade) to a strategy. Schwab does not echo our ``client_order_id``, so
the answer rests on the intent and on WHEN each listed order was entered, against two
windows derived from the row:

- the **found window** — when our order could have been in flight: from the write-ahead
  (``created_at``) to the unknown mark (``updated_at``, stamped after the send returned) but
  never later than ``created_at + max_send_duration``, ± clock skew. Only an order entered
  here can be adopted, so an identical order typed in by hand later is not ours;
- the **doubt window** — anywhere our order could have LANDED: the found window widened by
  the consistency window on both sides (a lost response can still be processed server-side
  after we gave up). Any order here that could be ours makes absence unprovable.

Rules: only ``unknown`` rows (``pending`` → NOT_SETTLED; ``resolve`` re-anchors them); both
answers wait out the consistency window since ``updated_at``; the listing is requested with
a wide margin and filtered locally on each order's own ``enteredTime``; it must include the
orders we placed in the doubt window, whose entry times also check our clock against the
broker's. FOUND needs exactly one candidate in the doubt window, inside the found window,
an exact match (canonical symbol, the exact instruction, quantity, type, LIMIT price, and
the DAY/NORMAL/SINGLE shape we always send), not a REPLACED original, and no other
unresolved local order that could own it. ABSENT needs no candidate and no unparseable
order that might be one. Everything else is INCONCLUSIVE with a machine-readable ``code``
(``WINDOW_OPEN`` = retry later). An identical order entered by someone else inside the
found window cannot be told apart from ours — the go-live runbook forbids manual trading in
the traded symbols for that reason.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from trader.core.enums import OrderType
from trader.core.protocols import Clock
from trader.execution.idempotency import UNKNOWN, OrderRecord, ReconcileResult
from trader.observability.logging import get_logger
from trader.schwab.errors import SchwabError
from trader.schwab.orders import SchwabOrderStatus, SchwabTradingClient

# Reason codes on INCONCLUSIVE results.
WINDOW_OPEN = "window_open"  # our order may not be visible yet — retry after the window
NOT_SETTLED = "not_settled"  # not an 'unknown' row (e.g. pending: resolve re-anchors it)
AMBIGUOUS = "ambiguous"  # a listed order might be ours but can't be proven to be
LISTING_FAILED = "listing_failed"  # the listing errored or may be truncated
LISTING_INCOMPLETE = "listing_incomplete"  # an order we know of is missing from the listing
CLOCK_SKEW = "clock_skew"  # our known orders' entry times disagree with our clock
TOO_OLD = "too_old"  # beyond the broker's listing look-back: resolve manually

LOOKBACK_LIMIT = timedelta(days=59)  # Schwab lists at most ~60 days back [VERIFY]
LISTING_MARGIN = timedelta(days=1)  # server-side bounds are only a coarse prefilter
PRICE_TICK = Decimal("0.01")  # a LIMIT price within a tick may be the broker's rounding
# The order shape we always send (schwab.orders.build_order_json).
_SENT_DURATION, _SENT_SESSION, _SENT_STRATEGY = "DAY", "NORMAL", "SINGLE"

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
        bound_orders_created_between: Callable[[datetime, datetime], Mapping[str, datetime]],
        awaiting_resolution: Callable[[], Collection[OrderRecord]],
        consistency_window: timedelta = timedelta(minutes=5),
        clock_skew: timedelta = timedelta(minutes=2),
        max_send_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if consistency_window <= timedelta(0):
            raise ValueError("consistency_window must be positive")
        if clock_skew < timedelta(0):
            raise ValueError("clock_skew must be non-negative")
        if max_send_duration <= timedelta(0):
            raise ValueError("max_send_duration must be positive")
        self._client = client
        self._account = account_hash
        self._clock = clock
        self._bound_broker_ids = bound_broker_ids
        self._bound_created_between = bound_orders_created_between
        self._awaiting_resolution = awaiting_resolution
        self._window = consistency_window
        self._skew = clock_skew
        self._max_send = max_send_duration

    def found_window(self, record: OrderRecord) -> tuple[datetime, datetime]:
        """When ``record``'s order could have been in flight (± skew)."""
        in_flight_until = record.created_at + self._max_send
        if record.status == UNKNOWN:
            in_flight_until = min(record.updated_at, in_flight_until)
        return record.created_at - self._skew, in_flight_until + self._skew

    def doubt_window(self, record: OrderRecord) -> tuple[datetime, datetime]:
        """Anywhere ``record``'s order could have landed: the found window widened by the
        consistency window on both sides (and never ending before ``updated_at``)."""
        lo, hi = self.found_window(record)
        return lo - self._window, max(hi, record.updated_at + self._skew) + self._window

    def __call__(self, record: OrderRecord) -> ReconcileResult:
        if record.status != UNKNOWN:
            return ReconcileResult.inconclusive(
                f"status {record.status!r} has no settled send window yet", NOT_SETTLED
            )
        snapshot = self._clock.now()  # the local time the listing is requested
        waited = snapshot - record.updated_at
        if waited < self._window:
            return ReconcileResult.inconclusive(
                f"inside the consistency window ({waited.total_seconds():.0f}s elapsed)",
                WINDOW_OPEN,
            )
        found_lo, found_hi = self.found_window(record)
        doubt_lo, doubt_hi = self.doubt_window(record)
        if snapshot - doubt_lo > LOOKBACK_LIMIT:
            return ReconcileResult.inconclusive(
                "the order is older than the broker's listing look-back", TOO_OLD
            )
        try:
            listing = self._client.get_orders(
                self._account,
                from_entered=max(doubt_lo - LISTING_MARGIN, snapshot - LOOKBACK_LIMIT),
                to_entered=doubt_hi + LISTING_MARGIN,
            )
        except (SchwabError, httpx.HTTPError, OSError) as exc:  # proves nothing
            return ReconcileResult.inconclusive(
                f"order listing failed ({type(exc).__name__})", LISTING_FAILED
            )

        # Self-checks with the orders we know we placed near this one.
        listed = {o.order_id: o for o in listing.orders}
        known = self._bound_created_between(doubt_lo, doubt_hi)
        present = listed.keys() | {u.order_id for u in listing.unparsed}
        missing = set(known) - present
        if missing:
            _log.error(
                "order listing is missing orders we placed; not trusting it",
                cid=record.client_order_id,
                missing=len(missing),
            )
            return ReconcileResult.inconclusive(
                f"{len(missing)} known order(s) missing from the listing", LISTING_INCOMPLETE
            )
        for broker_order_id, written_at in known.items():
            entered = listed[broker_order_id].entered_time if broker_order_id in listed else None
            if entered is not None and not (
                written_at - self._skew <= entered <= written_at + self._max_send + self._skew
            ):
                _log.error("broker entry times disagree with our clock", cid=record.client_order_id)
                return ReconcileResult.inconclusive(
                    "a known order's entry time is outside its send window", CLOCK_SKEW
                )

        bound = set(self._bound_broker_ids())
        doubt = [
            o
            for o in listing.orders
            if o.order_id not in bound
            and _could_be(o, record)
            and _entered_within(o, doubt_lo, doubt_hi)
        ]
        if any(
            u.order_id not in bound
            and _canon(u.symbol) in ("", _canon(record.symbol))
            and (u.entered_time is None or doubt_lo <= u.entered_time <= doubt_hi)
            for u in listing.unparsed
        ):
            return ReconcileResult.inconclusive(
                "an unparseable listed order may be this one", AMBIGUOUS
            )
        if not doubt:
            return ReconcileResult.absent("no listed order could be this one")
        if len(doubt) > 1:
            return ReconcileResult.inconclusive(
                f"{len(doubt)} listed orders may be this one", AMBIGUOUS
            )
        (only,) = doubt
        if (
            not _exact_match(only, record, found_lo, found_hi)
            or only.raw_status.upper() == "REPLACED"
        ):
            return ReconcileResult.inconclusive("a similar listed order may be this one", AMBIGUOUS)
        for rival in self._awaiting_resolution():
            if rival.client_order_id == record.client_order_id or not _could_be(only, rival):
                continue
            rival_lo, rival_hi = self.doubt_window(rival)
            if _entered_within(only, rival_lo, rival_hi):
                return ReconcileResult.inconclusive(
                    "another unresolved local order may own the matching order", AMBIGUOUS
                )
        return ReconcileResult.found(only.order_id, "unique intent match in the found window")


def _canon(symbol: str) -> str:
    """Comparable form of a ticker: trimmed, upper-case, share-class separator unified."""
    return symbol.strip().upper().replace("/", ".")


def _entered_within(o: SchwabOrderStatus, lo: datetime, hi: datetime) -> bool:
    """Entered inside [lo, hi] — or of unknown entry time (which can't be ruled out)."""
    return o.entered_time is None or lo <= o.entered_time <= hi


def _exact_match(o: SchwabOrderStatus, record: OrderRecord, lo: datetime, hi: datetime) -> bool:
    """Every field known and equal to the intent (and to the order shape we always send),
    entered inside the found window."""
    return (
        _canon(o.symbol) == _canon(record.symbol)
        and o.instruction == record.side.value
        and o.quantity == record.quantity
        and o.leg_quantity in (0, record.quantity)
        and o.order_type == record.order_type.value
        and (record.order_type is not OrderType.LIMIT or o.price == record.limit_price)
        and o.duration == record.tif.value == _SENT_DURATION
        and o.session == _SENT_SESSION
        and o.strategy_type == _SENT_STRATEGY
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
    "CLOCK_SKEW",
    "LISTING_FAILED",
    "LISTING_INCOMPLETE",
    "NOT_SETTLED",
    "TOO_OLD",
    "WINDOW_OPEN",
    "SchwabOrderReconciler",
]
