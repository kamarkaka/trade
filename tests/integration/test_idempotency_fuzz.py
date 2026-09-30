"""Hypothesis fuzz: at-most-once order placement under randomized timeout / rejection /
crash / retry interleavings (M5.3 + LR3). The core real-money safety property — a naive
retry must never double a real position.

Two reconcilers are exercised:

* a PERFECT one (``FakeBroker.find_by_client_id``: exact + synchronous), and
* a LAGGING one that behaves like a real, eventually-consistent order listing: an order that
  landed only becomes visible ``lag`` ticks later, and "nothing visible" is reported as
  ABSENT only once the consistency ``WINDOW`` has elapsed since the row's ``updated_at``
  (at/after the last send); before that it is INCONCLUSIVE. With ``lag <= WINDOW`` the
  placement layer must still never double the order, must refuse (not re-send) while the
  outcome is unknown, and must eventually place the intent exactly once.
"""

import contextlib
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from fakes import FakeBroker
from trader.core import Order
from trader.core.enums import OrderType, Side
from trader.execution.idempotency import (
    OrderOutcomeUnknownError,
    OrderRecord,
    OrderRepository,
    ReconcileResult,
    place_idempotent,
)
from trader.state.db import connect
from trader.state.migrate import run_migrations

CID = "fuzz-cid-1"
BASE = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)
WINDOW = 5  # ticks the lagging reconciler waits (from updated_at) before trusting "absent"

# ok: lands normally. timeout_landed: reaches the broker but the response is lost.
# timeout_lost: fails before reaching the broker. reject: definitely not placed.
# crash: restart (durable DB survives). crash_mid_send: the order lands and the process dies
# before recording anything (row stays 'pending'). tick: time passes.
_ACTIONS = ["ok", "timeout_landed", "timeout_lost", "reject", "crash", "crash_mid_send", "tick"]


class _HardCrash(BaseException):
    """Process death mid-send: not an ``Exception``, so nothing in the code under test can
    catch it and record an outcome."""


class _CrashingBroker(FakeBroker):
    crash_after_landing = False

    def submit_order(self, order: Order) -> str:
        broker_order_id = super().submit_order(order)
        if self.crash_after_landing:
            self.crash_after_landing = False
            raise _HardCrash
        return broker_order_id


def _order() -> Order:
    return Order(CID, "s1", "AAPL", Side.BUY, 10, OrderType.MARKET)


def _landed(broker: FakeBroker) -> int:
    return sum(1 for f in broker._fills.values() if f.client_order_id == CID)


def _perfect(broker: FakeBroker):  # type: ignore[no-untyped-def]
    def reconcile(record: OrderRecord) -> ReconcileResult:
        fill = broker.find_by_client_id(record.client_order_id)
        return ReconcileResult.found(fill.broker_order_id) if fill else ReconcileResult.absent()

    return reconcile


def _attempt(broker: _CrashingBroker, repo: OrderRepository, action: str, reconcile) -> None:  # type: ignore[no-untyped-def]
    broker.fail_next_submit = action in ("timeout_landed", "timeout_lost")
    broker.record_on_timeout = action == "timeout_landed"
    broker.reject_next_submit = action == "reject"
    broker.crash_after_landing = action == "crash_mid_send"
    place_idempotent(broker, repo, _order(), reconcile=reconcile)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(_ACTIONS), min_size=1, max_size=14))
def test_at_most_once_with_a_perfect_reconciler(actions: list[str]) -> None:
    conn = connect(":memory:")
    run_migrations(conn)
    broker = _CrashingBroker()
    repo = OrderRepository(conn)
    for action in actions:
        if action in ("crash", "tick"):
            repo = OrderRepository(conn) if action == "crash" else repo
            continue
        with contextlib.suppress(Exception, _HardCrash):
            _attempt(broker, repo, action, _perfect(broker))
        assert _landed(broker) <= 1  # THE property, after every step
    if any(a in ("ok", "timeout_landed", "crash_mid_send") for a in actions):
        assert _landed(broker) == 1


@settings(max_examples=400, deadline=None)
@given(
    actions=st.lists(st.sampled_from(_ACTIONS), min_size=1, max_size=16),
    lag=st.integers(min_value=0, max_value=WINDOW),
    tick=st.integers(min_value=1, max_value=3),
)
def test_at_most_once_with_a_lagging_reconciler(actions: list[str], lag: int, tick: int) -> None:
    conn = connect(":memory:")
    run_migrations(conn)
    clock = {"t": 0}
    now = lambda: BASE + timedelta(seconds=clock["t"])  # noqa: E731
    broker = _CrashingBroker()
    repo = OrderRepository(conn, now=now)
    landed_at: list[int] = []

    def lagging(record: OrderRecord) -> ReconcileResult:
        fill = broker.find_by_client_id(record.client_order_id)
        if fill is not None and clock["t"] - landed_at[0] >= lag:
            return ReconcileResult.found(fill.broker_order_id)  # visible after the lag
        waited = (now() - record.updated_at).total_seconds()
        if waited >= WINDOW:
            return ReconcileResult.absent("window elapsed, nothing listed")
        return ReconcileResult.inconclusive("inside the consistency window")

    def step(action: str) -> None:
        nonlocal repo
        if action == "tick":
            clock["t"] += tick
            return
        if action == "crash":
            repo = OrderRepository(conn, now=now)
            return
        sent_before, landed_before = len(broker.submitted), _landed(broker)
        try:
            _attempt(broker, repo, action, lagging)
        except OrderOutcomeUnknownError as exc:
            if "inconclusive" in exc.detail:
                assert len(broker.submitted) == sent_before  # refused: nothing was sent
        except (Exception, _HardCrash):
            pass
        if _landed(broker) > landed_before:
            landed_at.append(clock["t"])
        assert _landed(broker) <= 1  # THE property, after every step

    for action in actions:
        step(action)

    # Liveness: once the window has passed, one more attempt resolves the intent — it adopts
    # the landed order, or places it — and the order exists at the broker exactly once.
    clock["t"] += WINDOW + lag
    step("ok")
    assert _landed(broker) == 1
