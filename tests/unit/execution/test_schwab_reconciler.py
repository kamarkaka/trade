"""SchwabOrderReconciler (LR4): only 'unknown' rows; our order must have been entered inside
the row's send window; both answers wait out the consistency window; FOUND only for a single
exact candidate no local rival could own; ABSENT only when nothing could be ours."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
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
T0 = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)  # write-ahead
SENT = T0 + timedelta(seconds=30)  # the send timed out and was marked unknown
WINDOW = timedelta(minutes=5)
SKEW = timedelta(minutes=2)
LATE = SENT + WINDOW + timedelta(seconds=1)  # the consistency window has elapsed
EARLY = SENT + timedelta(seconds=30)  # still inside it
IN_SEND_WINDOW = T0 + timedelta(seconds=10)


def _record(
    *,
    cid: str = "c1",
    status: str = "unknown",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit: Decimal | None = None,
    symbol: str = "AAPL",
    created: datetime = T0,
    updated: datetime = SENT,
    strategy: str = "s1",
) -> OrderRecord:
    return OrderRecord(
        client_order_id=cid,
        strategy_id=strategy,
        symbol=symbol,
        side=side,
        quantity=10,
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
    entered: datetime | None = IN_SEND_WINDOW,
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
        leg_quantity=leg_quantity,
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
    bound_in_window: tuple[str, ...] = (),
    awaiting: tuple[OrderRecord, ...] = (),
):  # type: ignore[no-untyped-def]
    record = record or _record()
    client = _Client(listing)
    reconciler = SchwabOrderReconciler(
        client,  # type: ignore[arg-type]
        ACCT,
        clock=FakeClock(at),
        bound_broker_ids=lambda: bound,
        bound_broker_ids_created_between=lambda lo, hi: bound_in_window,
        awaiting_resolution=lambda: (record, *awaiting),
        consistency_window=WINDOW,
        clock_skew=SKEW,
    )
    result = reconciler(record)
    return result, client


def _one(
    *orders: SchwabOrderStatus, unparsed: tuple[SchwabUnparsedOrder, ...] = ()
) -> OrderListing:
    return OrderListing(tuple(orders), unparsed)


# --- scope and listing ------------------------------------------------------------ #


def test_only_unknown_rows_are_reconciled() -> None:
    result, client = _reconcile(_one(_listed()), _record(status="pending"))
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == NOT_SETTLED
    assert client.calls == []


def test_the_listing_is_requested_wide_and_filtered_locally() -> None:
    _, client = _reconcile(_one())
    assert client.calls == [(ACCT, T0 - SKEW - timedelta(days=1), LATE + timedelta(days=1))]


def test_look_back_limit() -> None:
    old = T0 - timedelta(days=60)
    result, client = _reconcile(_one(), _record(created=old, updated=old + timedelta(seconds=30)))
    assert result.code == TOO_OLD and client.calls == []
    edge = LATE - timedelta(days=58, hours=12)  # within the limit: from-bound capped at 59d
    _, client = _reconcile(_one(), _record(created=edge, updated=edge + timedelta(seconds=30)))
    assert client.calls[0][1] == LATE - timedelta(days=59)


def test_a_failed_or_truncated_listing_is_inconclusive_without_leaking_detail() -> None:
    truncated = SchwabBadResponseError(f"accounts/{ACCT}/orders returned 3000+ orders")
    result, _ = _reconcile(truncated)
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == LISTING_FAILED
    assert ACCT not in result.detail  # never echo the exception text (account identifiers)


def test_a_listing_missing_an_order_we_know_is_not_trusted() -> None:
    result, _ = _reconcile(_one(), bound_in_window=("SCH-9",))
    assert result.code == LISTING_INCOMPLETE
    listed = _one(_listed("SCH-9", symbol="MSFT"))  # the known order is present: proceed
    assert _reconcile(listed, bound_in_window=("SCH-9",), bound=("SCH-9",))[0].outcome is (
        ReconcileOutcome.ABSENT
    )


# --- the consistency window ------------------------------------------------------- #


def test_neither_answer_before_the_window_elapses() -> None:
    # Our order may simply not be visible yet: an identical listed order could be a manual
    # trade, and an empty listing proves nothing.
    assert _reconcile(_one(_listed()), at=EARLY)[0].code == WINDOW_OPEN
    assert _reconcile(_one(), at=EARLY)[0].code == WINDOW_OPEN


def test_the_window_is_measured_from_updated_at() -> None:
    re_anchored = _record(updated=LATE - timedelta(minutes=1))  # e.g. after a crash re-anchor
    assert _reconcile(_one(), re_anchored)[0].code == WINDOW_OPEN


# --- FOUND ------------------------------------------------------------------------ #


def test_a_single_exact_candidate_in_the_send_window_is_found() -> None:
    result, _ = _reconcile(_one(_listed()))
    assert result.outcome is ReconcileOutcome.FOUND and result.broker_order_id == "SCH-1"


def test_symbols_are_compared_canonically() -> None:
    listed = _listed(symbol="brk/b")
    result, _ = _reconcile(_one(listed), _record(symbol="BRK.B"))
    assert result.outcome is ReconcileOutcome.FOUND


def test_an_identical_order_entered_outside_the_send_window_is_not_ours() -> None:
    manual = _listed(entered=SENT + SKEW + timedelta(minutes=5))  # typed in by hand later
    assert _reconcile(_one(manual))[0].outcome is ReconcileOutcome.ABSENT
    before = _listed(entered=T0 - SKEW - timedelta(seconds=1))
    assert _reconcile(_one(before))[0].outcome is ReconcileOutcome.ABSENT


def test_a_second_possible_candidate_blocks_found() -> None:
    maybe = _listed("SCH-2", entered=None)  # can't rule it out
    result, _ = _reconcile(_one(_listed(), maybe))
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == AMBIGUOUS


def test_orders_bound_to_other_local_orders_are_ignored() -> None:
    result, _ = _reconcile(_one(_listed("SCH-OTHER")), bound=("SCH-OTHER",))
    assert result.outcome is ReconcileOutcome.ABSENT


def test_exact_limit_match_requires_the_same_price() -> None:
    record = _record(order_type=OrderType.LIMIT, limit=Decimal("150.25"))
    same = _listed(order_type="LIMIT", price=Decimal("150.25"))
    assert _reconcile(_one(same), record)[0].outcome is ReconcileOutcome.FOUND
    rounded = _listed(order_type="LIMIT", price=Decimal("150.26"))  # within a tick: maybe ours
    assert _reconcile(_one(rounded), record)[0].code == AMBIGUOUS
    far = _listed(order_type="LIMIT", price=Decimal("150.50"))  # beyond a tick: not ours
    assert _reconcile(_one(far), record)[0].outcome is ReconcileOutcome.ABSENT


# --- doubt blocks ABSENT ---------------------------------------------------------- #


@pytest.mark.parametrize(
    "similar",
    [
        _listed(instruction="SELL_SHORT", side=Side.SELL),  # same book side, other spelling
        _listed(entered=None),  # entry time unknown
        _listed(quantity=0, leg_quantity=0),  # quantity unknown
        _listed(quantity=5, leg_quantity=10),  # the leg carries our quantity
        _listed(order_type=""),  # order type unknown
        _listed(symbol="", instruction="", side=None),  # symbol and side unknown
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


def test_unparseable_orders_that_might_be_ours_block_absent() -> None:
    same = _one(unparsed=(SchwabUnparsedOrder("9", "AAPL", "bad quantity"),))
    unknown = _one(unparsed=(SchwabUnparsedOrder("9", "", "junk"),))
    other = _one(unparsed=(SchwabUnparsedOrder("9", "NVDA", "fractional"),))
    ours_already = _one(unparsed=(SchwabUnparsedOrder("9", "AAPL", "bad"),))
    assert _reconcile(same)[0].code == AMBIGUOUS
    assert _reconcile(unknown)[0].code == AMBIGUOUS
    assert _reconcile(other)[0].outcome is ReconcileOutcome.ABSENT
    assert _reconcile(ours_already, bound=("9",))[0].outcome is ReconcileOutcome.ABSENT


# --- local rivals ----------------------------------------------------------------- #


def test_a_rival_that_could_own_the_order_blocks_found() -> None:
    rival = _record(cid="c2", strategy="s2")  # same intent, another strategy, same window
    result, _ = _reconcile(_one(_listed()), awaiting=(rival,))
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE and result.code == AMBIGUOUS


def test_a_pending_rival_owns_everything_after_its_write_ahead() -> None:
    rival = _record(cid="c2", status="pending", created=T0 - timedelta(minutes=1), updated=T0)
    assert _reconcile(_one(_listed()), awaiting=(rival,))[0].code == AMBIGUOUS


def test_rivals_with_disjoint_send_windows_do_not_deadlock() -> None:
    later = T0 + timedelta(hours=1)
    rival = _record(cid="c2", created=later, updated=later + timedelta(seconds=30))
    result, _ = _reconcile(_one(_listed()), awaiting=(rival,))
    assert result.outcome is ReconcileOutcome.FOUND


def test_a_different_intent_is_not_a_rival() -> None:
    rival = _record(cid="c3", side=Side.SELL)
    assert _reconcile(_one(_listed()), awaiting=(rival,))[0].outcome is ReconcileOutcome.FOUND


def test_constructor_validation() -> None:
    kwargs = {
        "clock": FakeClock(T0),
        "bound_broker_ids": tuple,
        "bound_broker_ids_created_between": lambda lo, hi: (),
        "awaiting_resolution": tuple,
    }
    with pytest.raises(ValueError, match="window"):
        SchwabOrderReconciler(_Client(_one()), ACCT, consistency_window=timedelta(0), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="skew"):
        SchwabOrderReconciler(_Client(_one()), ACCT, clock_skew=timedelta(-1), **kwargs)  # type: ignore[arg-type]


# --- end to end: respx-mocked Schwab + real repository + resolve() ------------------- #


@respx.mock
def test_resolve_adopts_the_order_found_in_a_real_listing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    clock = {"now": T0}
    repo = OrderRepository(conn, now=lambda: clock["now"])
    from trader.core import Order

    order = Order("c1", "s1", "AAPL", Side.BUY, 10, OrderType.MARKET)
    repo._write_pending(order)
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
            bound_broker_ids_created_between=repo.bound_broker_ids_created_between,
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
