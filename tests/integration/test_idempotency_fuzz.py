"""Hypothesis fuzz: at-most-once order placement under randomized send outcomes, send
latency, hard crashes at every point, retries, and resolution passes (M5.3 + LR3).

The reconciler is LAGGING, like a real eventually-consistent order listing: an order that
landed becomes visible only ``lag`` seconds later, and "nothing visible" is reported as
ABSENT only once the consistency ``WINDOW`` has elapsed since the row's ``updated_at``;
otherwise INCONCLUSIVE (the documented Reconciler contract, with ``lag <= WINDOW``).

Properties checked after EVERY step:
- the order is sent at most once and lands at most once — ever;
- the durable row never contradicts the broker: ``not_placed`` only if nothing landed, and
  a recorded broker id is exactly the landed order's id.
Liveness: once time passes, resolution settles the row to the truth (placed iff landed).
"""

from datetime import UTC, datetime, timedelta

from hypothesis import event, given, settings
from hypothesis import strategies as st

from fakes import FakeBroker
from trader.core import Order, OrderNotPlacedError
from trader.core.enums import OrderType, Side
from trader.execution.idempotency import (
    NOT_PLACED,
    PLACED,
    OrderOutcomeUnknownError,
    OrderRecord,
    OrderRepository,
    ReconcileResult,
    ResolveOutcome,
    place_idempotent,
    resolve,
)
from trader.state.db import connect
from trader.state.migrate import run_migrations

CID = "fuzz-cid-1"
BASE = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)
WINDOW = 10  # seconds the reconciler waits (from updated_at) before trusting "absent"

# How the single send can go. crash_* are hard process deaths (nothing records an outcome):
# before the request leaves, after it lands, or after the broker's answer arrives.
_SENDS = [
    "ok",
    "timeout_landed",
    "timeout_lost",
    "reject",
    "crash_before_send",
    "crash_after_landing",
    "crash_after_return",
]


class _HardCrash(BaseException):
    """Process death: not an ``Exception``, so no code under test can catch it."""


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> datetime:
        return BASE + timedelta(seconds=self.t)


class _Broker(FakeBroker):
    """FakeBroker whose send takes time and can crash at chosen points; records landings."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self.clock = clock
        self.delay = 0.0
        self.crash: str | None = None
        self.landed_at: list[float] = []

    def submit_order(self, order: Order) -> str:
        if self.crash == "crash_before_send":
            raise _HardCrash
        self.clock.t += self.delay  # the request takes time (rate limiter, network)
        landed_before = self.find_by_client_id(order.client_order_id) is not None
        try:
            broker_order_id = super().submit_order(order)
        finally:
            if not landed_before and self.find_by_client_id(order.client_order_id) is not None:
                self.landed_at.append(self.clock.t)
        if self.crash == "crash_after_landing":
            raise _HardCrash
        return broker_order_id


class _Repo(OrderRepository):
    crash_on_record = False

    def record_placed(self, client_order_id: str, broker_order_id: str) -> str | None:
        if self.crash_on_record:
            raise _HardCrash  # the broker answered; the process died before recording
        return super().record_placed(client_order_id, broker_order_id)


def _order() -> Order:
    return Order(CID, "s1", "AAPL", Side.BUY, 10, OrderType.MARKET)


@settings(max_examples=500, deadline=None)
@given(
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("send"), st.sampled_from(_SENDS), st.integers(0, 2 * WINDOW)),
            st.tuples(st.just("resolve"), st.just(""), st.just(0)),
            st.tuples(st.just("tick"), st.just(""), st.integers(1, 2 * WINDOW)),
        ),
        min_size=1,
        max_size=14,
    ),
    lag=st.integers(min_value=0, max_value=WINDOW),
)
def test_at_most_once_and_truthful_rows_under_a_lagging_reconciler(
    steps: list[tuple[str, str, int]], lag: int
) -> None:
    conn = connect(":memory:")
    run_migrations(conn)
    clock = _Clock()
    broker = _Broker(clock)
    repo = _Repo(conn, now=clock.now)

    def lagging(record: OrderRecord) -> ReconcileResult:
        snapshot = clock.t  # the listing is taken now
        fill = broker.find_by_client_id(record.client_order_id)
        if fill is not None and snapshot - broker.landed_at[0] >= lag:
            return ReconcileResult.found(fill.broker_order_id)
        waited = (clock.now() - record.updated_at).total_seconds()
        if waited >= WINDOW:
            return ReconcileResult.absent("window elapsed; nothing listed")
        return ReconcileResult.inconclusive("inside the consistency window")

    def check() -> None:
        assert len(broker.submitted) <= 1  # ONE send per client_order_id, ever
        landed = broker.find_by_client_id(CID)
        record = repo.get(CID)
        if record is not None and record.status == NOT_PLACED:
            assert landed is None  # never leave a live order recorded as not placed
        if record is not None and record.broker_order_id is not None:
            assert landed is not None and record.broker_order_id == landed.broker_order_id

    def do(kind: str, how: str, amount: int) -> None:
        if kind == "tick":
            clock.t += amount
            return
        if kind == "resolve":
            record = repo.get(CID)
            if record is not None:
                event(f"resolve: {resolve(repo, record, reconcile=lagging).outcome.value}")
            return
        broker.fail_next_submit = how in ("timeout_landed", "timeout_lost")
        broker.record_on_timeout = how == "timeout_landed"
        broker.reject_next_submit = how == "reject"
        broker.crash = how if how.startswith("crash_") and how != "crash_after_return" else None
        broker.delay = amount
        repo.crash_on_record = how == "crash_after_return"
        try:
            place_idempotent(broker, repo, _order(), reconcile=lagging)
            event("send: returned an id")
        except (OrderOutcomeUnknownError, OrderNotPlacedError, _HardCrash) as exc:
            event(f"send: {type(exc).__name__}")
        finally:
            broker.crash, repo.crash_on_record = None, False

    for kind, how, amount in steps:
        do(kind, how, amount)
        check()

    # Liveness: two resolution passes a window apart (the first may re-anchor an
    # interrupted send) settle the row to the truth.
    if repo.get(CID) is None:
        return
    for _ in range(2):
        clock.t += WINDOW + lag + 1
        do("resolve", "", 0)
        check()
    record = repo.get(CID)
    assert record is not None
    if broker.find_by_client_id(CID) is not None:
        assert record.status == PLACED and record.broker_order_id is not None
    else:
        assert record.status == NOT_PLACED
    assert resolve(repo, record, reconcile=lagging).outcome in (
        ResolveOutcome.PLACED,
        ResolveOutcome.NOT_PLACED,
    )
