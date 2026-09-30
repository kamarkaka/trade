"""SchwabOrderReconciler (LR4): only 'unknown' rows; both answers wait out the consistency
window; FOUND only for the single candidate anywhere our order could have landed (the doubt
window), entered while it could have been in flight (the found window), matching exactly and
ownable by no local rival; ABSENT only when nothing in the doubt window could be ours."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.core import Order
from trader.core.enums import OrderStatus, OrderType, Side, TimeInForce
from trader.execution.idempotency import (
    OrderRecord,
    OrderRepository,
    ReconcileOutcome,
    ResolveOutcome,
    resolve,
)
from trader.execution.schwab_reconciler import (
    AMBIGUOUS,
    CLOCK_SKEW,
    LISTING_FAILED,
    LISTING_INCOMPLETE,
    NOT_SETTLED,
    TOO_OLD,
    WINDOW_OPEN,
    SchwabOrderReconciler,
)
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import ACCOUNTS_PATH
from trader.schwab.errors import SchwabBadResponseError
from trader.schwab.http import SchwabHttp
from trader.schwab.orders import (
    OrderListing,
    SchwabOrderStatus,
    SchwabTradingClient,
    SchwabUnparsedOrder,
)
from trader.state.db import connect
from trader.state.migrate import run_migrations

ACCT = "HASHEDACCT"
WINDOW = timedelta(minutes=5)
SKEW = timedelta(minutes=2)
MAX_SEND = timedelta(minutes=5)
T0 = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)  # write-ahead
SENT = T0 + timedelta(seconds=30)  # the send timed out and was marked unknown
FOUND_HI = SENT + SKEW  # min(updated_at, created_at + max_send) + skew
DOUBT_LO = T0 - SKEW - WINDOW
DOUBT_HI = FOUND_HI + WINDOW
LATE = SENT + WINDOW + timedelta(seconds=1)  # the consistency window has elapsed
EARLY = SENT + timedelta(seconds=30)  # still inside it
IN_FLIGHT = T0 + timedelta(seconds=10)


def _record(
    *,
    cid: str = "c1",
    status: str = "unknown",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit: Decimal | None = None,
    symbol: str = "AAPL",
    quantity: int = 10,
    created: datetime = T0,
    updated: datetime = SENT,
    strategy: str = "s1",
) -> OrderRecord:
    return OrderRecord(
        client_order_id=cid,
        strategy_id=strategy,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit,
        tif=TimeInForce.DAY,
        status=status,
        broker_order_id=None,
        created_at=created,
        updated_at=updated,
    )


def _listed(
    order_id: str = "SCH-1",
    *,
    symbol: str = "AAPL",
    instruction: str = "BUY",
    side: Side | None = Side.BUY,
    quantity: int = 10,
    leg_quantity: int = 10,
    order_type: str = "MARKET",
    price: Decimal | None = None,
    entered: datetime | None = IN_FLIGHT,
    raw_status: str = "FILLED",
    duration: str = "DAY",
    session: str = "NORMAL",
    strategy_type: str = "SINGLE",
) -> SchwabOrderStatus:
    return SchwabOrderStatus(
        order_id,
        OrderStatus.FILLED,
        symbol,
        quantity,
        quantity,
        Decimal("100"),
        raw_status,
        entered_time=entered,
        instruction=instruction,
        side=side,
        order_type=order_type,
        price=price,
        leg_quantity=leg_quantity,
        duration=duration,
        session=session,
        strategy_type=strategy_type,
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
    record: OrderRecord | None = None,
    *,
    at: datetime = LATE,
    bound: tuple[str, ...] = (),
    known: dict[str, datetime] | None = None,
    awaiting: tuple[OrderRecord, ...] = (),
):  # type: ignore[no-untyped-def]
    record = record or _record()
    client = _Client(listing)
    reconciler = SchwabOrderReconciler(
        client,  # type: ignore[arg-type]
        ACCT,
        clock=FakeClock(at),
        bound_broker_ids=lambda: bound,
        bound_orders_created_between=lambda lo, hi: known or {},
        awaiting_resolution=lambda: (record, *awaiting),
        consistency_window=WINDOW,
        clock_skew=SKEW,
        max_send_duration=MAX_SEND,
    )
    return reconciler(record), client


def _one(
    *orders: SchwabOrderStatus, unparsed: tuple[SchwabUnparsedOrder, ...] = ()
) -> OrderListing:
    return OrderListing(tuple(orders), unparsed)


# --- scope, window, listing ---------------------------------------------------------- #


@pytest.mark.parametrize("status", ["pending", "WORKING", "not_placed", "FILLED"])
def test_only_unknown_rows_are_reconciled(status: str) -> None:
    result, client = _reconcile(_one(_listed()), _record(status=status))
    assert result.code == NOT_SETTLED and client.calls == []


def test_neither_answer_before_the_window_and_no_listing_call() -> None:
    result, client = _reconcile(_one(_listed()), at=EARLY)
    assert result.code == WINDOW_OPEN and client.calls == []  # don't even ask yet
    re_anchored = _record(updated=LATE - timedelta(minutes=1))  # measured from updated_at
    assert _reconcile(_one(), re_anchored)[0].code == WINDOW_OPEN


def test_the_listing_is_requested_wide_around_the_doubt_window() -> None:
    _, client = _reconcile(_one())
    assert client.calls == [(ACCT, DOUBT_LO - timedelta(days=1), DOUBT_HI + timedelta(days=1))]


def test_look_back_limit() -> None:
    old = T0 - timedelta(days=60)
    result, client = _reconcile(_one(), _record(created=old, updated=old + timedelta(seconds=30)))
    assert result.code == TOO_OLD and client.calls == []


@pytest.mark.parametrize(
    "error",
    [
        SchwabBadResponseError(f"accounts/{ACCT}/orders returned 3000+ orders"),
        httpx.ReadTimeout("slow", request=httpx.Request("GET", f"https://x/{ACCT}")),
        ConnectionError(f"reset while reading {ACCT}"),
    ],
    ids=type,
)
def test_a_failed_listing_is_inconclusive_without_echoing_its_text(error: Exception) -> None:
    result, _ = _reconcile(error)
    assert result.code == LISTING_FAILED and ACCT not in result.detail


def test_a_programming_error_is_not_mistaken_for_a_listing_failure() -> None:
    with pytest.raises(TypeError):
        _reconcile(TypeError("bug in the wiring"))  # surfaces (resolve() logs it)


def test_a_listing_missing_an_order_we_placed_is_not_trusted() -> None:
    result, _ = _reconcile(_one(), known={"SCH-9": T0})
    assert result.code == LISTING_INCOMPLETE
    ours = _listed("SCH-9", symbol="MSFT", entered=T0 + timedelta(seconds=5))
    assert _reconcile(_one(ours), known={"SCH-9": T0}, bound=("SCH-9",))[0].outcome is (
        ReconcileOutcome.ABSENT
    )


def test_our_own_orders_check_the_clock() -> None:
    drifted = _listed("SCH-9", symbol="MSFT", entered=T0 + timedelta(minutes=20))
    result, _ = _reconcile(_one(drifted), known={"SCH-9": T0}, bound=("SCH-9",))
    assert result.code == CLOCK_SKEW  # a known order's entry time is off its send window
    early = _listed("SCH-9", symbol="MSFT", entered=T0 - SKEW - timedelta(seconds=1))
    assert _reconcile(_one(early), known={"SCH-9": T0}, bound=("SCH-9",))[0].code == CLOCK_SKEW


# --- FOUND -------------------------------------------------------------------------- #


def test_a_single_exact_candidate_in_the_found_window_is_found() -> None:
    result, _ = _reconcile(_one(_listed()))
    assert result.outcome is ReconcileOutcome.FOUND and result.broker_order_id == "SCH-1"


def test_found_window_bounds_are_inclusive_and_include_the_skew() -> None:
    at_edge = _listed(entered=FOUND_HI)
    assert _reconcile(_one(at_edge))[0].outcome is ReconcileOutcome.FOUND
    past_edge = _listed(entered=FOUND_HI + timedelta(seconds=1))  # in doubt, not in flight
    assert _reconcile(_one(past_edge))[0].code == AMBIGUOUS
    at_low_edge = _listed(entered=T0 - SKEW)
    assert _reconcile(_one(at_low_edge))[0].outcome is ReconcileOutcome.FOUND


def test_a_late_landing_is_doubt_not_absence() -> None:
    # A lost response can still be processed server-side after we gave up: an order entered
    # after the found window but inside the doubt window must block ABSENT, not be ignored.
    late = _listed(entered=SENT + timedelta(minutes=2, seconds=30))
    result, _ = _reconcile(_one(late))
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == AMBIGUOUS
    far = _listed(entered=T0 + timedelta(hours=1))  # outside anywhere it could have landed
    assert _reconcile(_one(far))[0].outcome is ReconcileOutcome.ABSENT


def test_the_found_window_is_capped_by_the_maximum_send_time() -> None:
    # A row re-anchored hours after an interrupted send: an identical order typed in by hand
    # meanwhile must never be adopted as ours.
    record = _record(created=T0, updated=T0 + timedelta(hours=2))
    manual = _listed(entered=T0 + timedelta(hours=1))
    at = T0 + timedelta(hours=2) + WINDOW + timedelta(seconds=1)
    assert _reconcile(_one(manual), record, at=at)[0].code == AMBIGUOUS
    in_flight = _listed(entered=T0 + MAX_SEND + SKEW)  # the latest a real send could land
    assert _reconcile(_one(in_flight), record, at=at)[0].outcome is ReconcileOutcome.FOUND


def test_symbols_are_compared_canonically() -> None:
    result, _ = _reconcile(_one(_listed(symbol="brk/b")), _record(symbol="BRK.B"))
    assert result.outcome is ReconcileOutcome.FOUND


def test_a_second_possible_candidate_blocks_found() -> None:
    maybe = _listed("SCH-2", entered=None)
    assert _reconcile(_one(_listed(), maybe))[0].code == AMBIGUOUS


def test_orders_bound_to_other_local_orders_are_ignored() -> None:
    assert _reconcile(_one(_listed("X")), bound=("X",))[0].outcome is ReconcileOutcome.ABSENT


@pytest.mark.parametrize(
    "not_quite",
    [
        _listed(raw_status="REPLACED"),  # a replaced original: never adopt a dead order
        _listed(duration="GOOD_TILL_CANCEL"),  # we only send DAY
        _listed(session="SEAMLESS"),  # we only send NORMAL
        _listed(strategy_type="OCO"),  # we only send SINGLE
        _listed(duration=""),  # unknown shape: can't be exact
        _listed(quantity=10, leg_quantity=5),  # the leg disagrees
    ],
)
def test_a_similar_order_that_is_not_an_exact_match_is_ambiguous(
    not_quite: SchwabOrderStatus,
) -> None:
    result, _ = _reconcile(_one(not_quite))
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == AMBIGUOUS


def test_exact_limit_match_requires_the_same_price() -> None:
    record = _record(order_type=OrderType.LIMIT, limit=Decimal("150.25"))
    same = _listed(order_type="LIMIT", price=Decimal("150.25"))
    assert _reconcile(_one(same), record)[0].outcome is ReconcileOutcome.FOUND
    rounded = _listed(order_type="LIMIT", price=Decimal("150.26"))  # within a tick
    assert _reconcile(_one(rounded), record)[0].code == AMBIGUOUS
    far = _listed(order_type="LIMIT", price=Decimal("150.50"))  # beyond a tick
    assert _reconcile(_one(far), record)[0].outcome is ReconcileOutcome.ABSENT


# --- doubt blocks ABSENT ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "similar",
    [
        _listed(instruction="SELL_SHORT", side=Side.SELL),
        _listed(entered=None),
        _listed(quantity=0, leg_quantity=0),
        _listed(quantity=5, leg_quantity=10),
        _listed(order_type=""),
        _listed(symbol="", instruction="", side=None),
    ],
)
def test_an_order_that_could_be_ours_blocks_absent(similar: SchwabOrderStatus) -> None:
    record = _record(side=Side.SELL) if similar.side is Side.SELL else _record()
    result, _ = _reconcile(_one(similar), record)
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == AMBIGUOUS


@pytest.mark.parametrize(
    "different",
    [
        _listed(symbol="MSFT"),
        _listed(instruction="SELL", side=Side.SELL),
        _listed(quantity=11, leg_quantity=11),
        _listed(order_type="LIMIT", price=Decimal("1")),
    ],
)
def test_provably_different_orders_do_not_block_absent(different: SchwabOrderStatus) -> None:
    assert _reconcile(_one(different))[0].outcome is ReconcileOutcome.ABSENT


def test_unparseable_orders_block_only_where_they_might_be_ours() -> None:
    def unparsed(symbol: str, entered: datetime | None) -> OrderListing:
        return _one(unparsed=(SchwabUnparsedOrder("9", symbol, "bad", entered),))

    assert _reconcile(unparsed("AAPL", IN_FLIGHT))[0].code == AMBIGUOUS
    assert _reconcile(unparsed("aapl", IN_FLIGHT))[0].code == AMBIGUOUS  # canonical symbol
    assert _reconcile(unparsed("", IN_FLIGHT))[0].code == AMBIGUOUS  # unknown symbol
    assert _reconcile(unparsed("AAPL", None))[0].code == AMBIGUOUS  # unknown entry time
    # a recurring fractional Stock Slice at another time or in another symbol doesn't block
    slice_later = unparsed("AAPL", T0 + timedelta(hours=3))
    assert _reconcile(slice_later)[0].outcome is ReconcileOutcome.ABSENT
    assert _reconcile(unparsed("NVDA", IN_FLIGHT))[0].outcome is ReconcileOutcome.ABSENT
    ours = unparsed("AAPL", IN_FLIGHT)
    assert _reconcile(ours, bound=("9",))[0].outcome is ReconcileOutcome.ABSENT


# --- local rivals ------------------------------------------------------------------- #


def test_a_rival_that_could_own_the_order_blocks_found() -> None:
    rival = _record(cid="c2", strategy="s2")  # same intent, another strategy, same window
    assert _reconcile(_one(_listed()), awaiting=(rival,))[0].code == AMBIGUOUS


def test_a_rival_with_a_compatible_but_not_identical_intent_is_a_rival() -> None:
    record = _record(order_type=OrderType.LIMIT, limit=Decimal("150.25"))
    rival = _record(cid="c2", order_type=OrderType.LIMIT, limit=Decimal("150.26"))
    listed = _listed(order_type="LIMIT", price=Decimal("150.25"))
    assert _reconcile(_one(listed), record, awaiting=(rival,))[0].code == AMBIGUOUS
    canonical = _record(cid="c3", symbol="aapl")  # same ticker spelled differently
    assert _reconcile(_one(_listed()), awaiting=(canonical,))[0].code == AMBIGUOUS


@pytest.mark.parametrize(
    "not_a_rival",
    [
        _record(cid="c2", quantity=11),
        _record(cid="c2", symbol="MSFT"),
        _record(cid="c2", side=Side.SELL),
        _record(cid="c2", order_type=OrderType.LIMIT, limit=Decimal("99")),
    ],
)
def test_an_incompatible_rival_does_not_block(not_a_rival: OrderRecord) -> None:
    assert _reconcile(_one(_listed()), awaiting=(not_a_rival,))[0].outcome is (
        ReconcileOutcome.FOUND
    )


def test_rivals_are_bounded_by_where_their_own_order_could_have_landed() -> None:
    later = T0 + timedelta(hours=1)
    disjoint = _record(cid="c2", created=later, updated=later + timedelta(seconds=30))
    assert _reconcile(_one(_listed()), awaiting=(disjoint,))[0].outcome is ReconcileOutcome.FOUND
    # a pending rival's send could only have landed up to created + max send (+ skew/window)
    stale = _record(
        cid="c3", status="pending", created=T0 - timedelta(hours=3), updated=T0 - timedelta(hours=3)
    )
    assert _reconcile(_one(_listed()), awaiting=(stale,))[0].outcome is ReconcileOutcome.FOUND
    recent = _record(cid="c4", status="pending", created=T0 - timedelta(minutes=1), updated=T0)
    assert _reconcile(_one(_listed()), awaiting=(recent,))[0].code == AMBIGUOUS


def test_constructor_validation() -> None:
    kwargs = {
        "clock": FakeClock(T0),
        "bound_broker_ids": tuple,
        "bound_orders_created_between": lambda lo, hi: {},
        "awaiting_resolution": tuple,
    }
    client = _Client(_one())
    with pytest.raises(ValueError, match="window"):
        SchwabOrderReconciler(client, ACCT, consistency_window=timedelta(0), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="skew"):
        SchwabOrderReconciler(client, ACCT, clock_skew=timedelta(-1), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_send"):
        SchwabOrderReconciler(client, ACCT, max_send_duration=timedelta(0), **kwargs)  # type: ignore[arg-type]


# --- end to end: respx-mocked Schwab + real repository + resolve() ------------------- #


@respx.mock
def test_resolve_adopts_the_order_found_in_a_real_listing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    clock = {"now": T0}
    repo = OrderRepository(conn, now=lambda: clock["now"])
    repo._write_pending(Order("c1", "s1", "AAPL", Side.BUY, 10, OrderType.MARKET))
    clock["now"] = SENT
    repo.mark_unknown_after_send("c1")  # the send timed out
    respx.get(f"{ACCOUNTS_PATH}/{ACCT}/orders").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "orderId": "1003490104",
                    "status": "FILLED",
                    "enteredTime": "2026-06-29T14:00:12+0000",
                    "quantity": 10,
                    "filledQuantity": 10,
                    "orderType": "MARKET",
                    "duration": "DAY",
                    "session": "NORMAL",
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [
                        {"instruction": "BUY", "quantity": 10, "instrument": {"symbol": "AAPL"}}
                    ],
                }
            ],
        )
    )
    cfg = SchwabClientConfig(app_key="K", app_secret="S", token_store_path=tmp_path / "t.sqlite")
    store = TokenStore(tmp_path / "t.sqlite")
    store.save(TokenSet("ACC", "REF", LATE + timedelta(seconds=1800), LATE))
    with httpx.Client() as http_client:
        http = SchwabHttp(cfg, http_client, store, clock=FakeClock(LATE), sleep=lambda _s: None)
        reconciler = SchwabOrderReconciler(
            SchwabTradingClient(http),
            ACCT,
            clock=FakeClock(LATE),
            bound_broker_ids=repo.bound_broker_ids,
            bound_orders_created_between=repo.bound_orders_created_between,
            awaiting_resolution=repo.awaiting_resolution,
            consistency_window=WINDOW,
            clock_skew=SKEW,
        )
        record = repo.get("c1")
        assert record is not None
        result = resolve(repo, record, reconcile=reconciler)
    assert result.outcome is ResolveOutcome.PLACED and result.broker_order_id == "1003490104"
    row = repo.get("c1")
    assert row is not None and (row.status, row.broker_order_id) == ("WORKING", "1003490104")


def test_the_window_boundary_is_inclusive() -> None:
    at_boundary = SENT + WINDOW
    assert _reconcile(_one(), at=at_boundary)[0].outcome is ReconcileOutcome.ABSENT


def test_the_clock_check_allows_a_slow_send() -> None:
    slow = _listed("SCH-9", symbol="MSFT", entered=T0 + timedelta(minutes=3))  # within max send
    assert _reconcile(_one(slow), known={"SCH-9": T0}, bound=("SCH-9",))[0].outcome is (
        ReconcileOutcome.ABSENT
    )


def test_a_rival_blocks_where_its_order_could_have_landed_not_only_while_in_flight() -> None:
    # The candidate sits after this rival's in-flight window but inside where a lost
    # response of the rival could still have landed: the rival could own it.
    rival = _record(
        cid="c2", created=T0 - timedelta(minutes=4), updated=T0 - timedelta(minutes=3.5)
    )
    assert _reconcile(_one(_listed()), awaiting=(rival,))[0].code == AMBIGUOUS


def test_the_clock_check_tolerates_skew_in_both_directions() -> None:
    ahead = _listed("SCH-9", symbol="MSFT", entered=T0 - timedelta(minutes=1))  # within skew
    assert _reconcile(_one(ahead), known={"SCH-9": T0}, bound=("SCH-9",))[0].outcome is (
        ReconcileOutcome.ABSENT
    )
