"""SchwabOrderReconciler (LR4): FOUND only for a unique exact intent match, INCONCLUSIVE
whenever absence can't be proven, ABSENT only after the consistency window (anchored at the
row's updated_at)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fakes import FakeClock
from trader.core.enums import OrderStatus, OrderType, Side, TimeInForce
from trader.execution.idempotency import OrderRecord, ReconcileOutcome
from trader.execution.schwab_reconciler import SchwabOrderReconciler
from trader.schwab.errors import SchwabBadResponseError
from trader.schwab.orders import OrderListing, SchwabOrderStatus, SchwabUnparsedOrder

CREATED = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)
ACCT = "HASHEDACCT"
WINDOW = timedelta(minutes=5)
SKEW = timedelta(minutes=2)


def _record(
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit: Decimal | None = None,
    updated: datetime = CREATED,
) -> OrderRecord:
    return OrderRecord(
        client_order_id="c1",
        strategy_id="s1",
        symbol="AAPL",
        side=side,
        quantity=10,
        order_type=order_type,
        limit_price=limit,
        tif=TimeInForce.DAY,
        status="unknown",
        broker_order_id=None,
        created_at=CREATED,
        updated_at=updated,
    )


def _listed(
    order_id: str = "SCH-1",
    *,
    symbol: str = "AAPL",
    instruction: str = "BUY",
    side: Side | None = Side.BUY,
    quantity: int = 10,
    order_type: str = "MARKET",
    price: Decimal | None = None,
    entered: datetime | None = CREATED + timedelta(seconds=2),
) -> SchwabOrderStatus:
    return SchwabOrderStatus(
        order_id,
        OrderStatus.FILLED,
        symbol,
        quantity,
        quantity,
        Decimal("100"),
        "FILLED",
        entered_time=entered,
        instruction=instruction,
        side=side,
        order_type=order_type,
        price=price,
    )


class _Client:
    def __init__(self, listing: OrderListing | Exception) -> None:
        self._listing = listing
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_orders(
        self, account_hash: str, *, from_entered: datetime, to_entered: datetime
    ) -> OrderListing:
        self.calls.append((account_hash, from_entered, to_entered))
        if isinstance(self._listing, Exception):
            raise self._listing
        return self._listing


def _reconcile(
    listing: OrderListing | Exception,
    record: OrderRecord,
    *,
    at: datetime,
    bound: tuple[str, ...] = (),
) -> tuple[ReconcileOutcome, str | None, _Client]:
    client = _Client(listing)
    reconciler = SchwabOrderReconciler(
        client,  # type: ignore[arg-type]
        ACCT,
        clock=FakeClock(at),
        bound_broker_ids=lambda: bound,
        consistency_window=WINDOW,
        clock_skew=SKEW,
    )
    result = reconciler(record)
    return result.outcome, result.broker_order_id, client


LATE = CREATED + WINDOW + timedelta(seconds=1)  # the window has elapsed
EARLY = CREATED + timedelta(seconds=30)  # still inside the window


def test_lists_from_created_minus_skew_to_snapshot_plus_skew() -> None:
    _, _, client = _reconcile(OrderListing(()), _record(), at=LATE)
    assert client.calls == [(ACCT, CREATED - SKEW, LATE + SKEW)]


def test_unique_exact_match_is_found() -> None:
    outcome, broker_order_id, _ = _reconcile(OrderListing((_listed(),)), _record(), at=EARLY)
    assert outcome is ReconcileOutcome.FOUND and broker_order_id == "SCH-1"


def test_exact_limit_match_requires_the_same_price() -> None:
    record = _record(order_type=OrderType.LIMIT, limit=Decimal("150.25"))
    same = _listed(order_type="LIMIT", price=Decimal("150.25"))
    assert _reconcile(OrderListing((same,)), record, at=EARLY)[0] is ReconcileOutcome.FOUND
    other = _listed(order_type="LIMIT", price=Decimal("150.50"))  # provably different
    assert _reconcile(OrderListing((other,)), record, at=LATE)[0] is ReconcileOutcome.ABSENT
    unknown = _listed(order_type="LIMIT", price=None)  # can't rule it out
    assert _reconcile(OrderListing((unknown,)), record, at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE


def test_orders_bound_to_other_local_orders_are_ignored() -> None:
    listing = OrderListing((_listed("SCH-OTHER"),))
    assert (
        _reconcile(listing, _record(), at=LATE, bound=("SCH-OTHER",))[0] is ReconcileOutcome.ABSENT
    )


def test_several_exact_matches_are_ambiguous() -> None:
    listing = OrderListing((_listed("SCH-1"), _listed("SCH-2")))
    assert _reconcile(listing, _record(), at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE


@pytest.mark.parametrize(
    "similar",
    [
        _listed(instruction="SELL_SHORT", side=Side.SELL),  # same book side, other spelling
        _listed(entered=None),  # entered time unknown
        _listed(quantity=0),  # quantity unknown
        _listed(order_type=""),  # order type unknown
        _listed(symbol="", instruction="", side=None),  # symbol and side unknown
    ],
)
def test_an_order_that_could_be_ours_blocks_absent(similar: SchwabOrderStatus) -> None:
    record = _record(side=Side.SELL) if similar.side is Side.SELL else _record()
    assert _reconcile(OrderListing((similar,)), record, at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE


@pytest.mark.parametrize(
    "different",
    [
        _listed(symbol="MSFT"),
        _listed(instruction="SELL", side=Side.SELL),
        _listed(quantity=11),
        _listed(order_type="LIMIT", price=Decimal("1")),
    ],
)
def test_provably_different_orders_do_not_block_absent(different: SchwabOrderStatus) -> None:
    assert _reconcile(OrderListing((different,)), _record(), at=LATE)[0] is ReconcileOutcome.ABSENT


def test_unparseable_order_on_this_symbol_blocks_absent() -> None:
    same = OrderListing((), (SchwabUnparsedOrder("9", "AAPL", "bad quantity"),))
    unknown = OrderListing((), (SchwabUnparsedOrder("9", "", "junk"),))
    other = OrderListing((), (SchwabUnparsedOrder("9", "NVDA", "fractional"),))
    assert _reconcile(same, _record(), at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE
    assert _reconcile(unknown, _record(), at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE
    assert _reconcile(other, _record(), at=LATE)[0] is ReconcileOutcome.ABSENT


def test_a_failed_or_truncated_listing_is_inconclusive() -> None:
    truncated = SchwabBadResponseError("list-orders returned 3000+ orders; may be truncated")
    assert _reconcile(truncated, _record(), at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE
    assert (
        _reconcile(ConnectionError("down"), _record(), at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE
    )


def test_absent_only_after_the_window_measured_from_updated_at() -> None:
    empty = OrderListing(())
    assert _reconcile(empty, _record(), at=EARLY)[0] is ReconcileOutcome.INCONCLUSIVE
    assert _reconcile(empty, _record(), at=LATE)[0] is ReconcileOutcome.ABSENT
    # created long ago, but re-anchored (updated_at) a minute ago: still inside the window
    recent = _record(updated=LATE - timedelta(minutes=1))
    assert _reconcile(empty, recent, at=LATE)[0] is ReconcileOutcome.INCONCLUSIVE


def test_constructor_validation() -> None:
    kwargs = {"clock": FakeClock(CREATED), "bound_broker_ids": tuple}
    with pytest.raises(ValueError, match="window"):
        SchwabOrderReconciler(
            _Client(OrderListing(())), ACCT, consistency_window=timedelta(0), **kwargs
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="skew"):
        SchwabOrderReconciler(_Client(OrderListing(())), ACCT, clock_skew=timedelta(-1), **kwargs)  # type: ignore[arg-type]
