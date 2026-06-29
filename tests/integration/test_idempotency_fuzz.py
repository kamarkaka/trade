"""Hypothesis fuzz: at-most-once order placement under randomized timeout/crash/retry
interleavings (M5.3). The core real-money safety property — a naive retry must never
double a real position."""

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st

from fakes import FakeBroker
from trader.core import Order
from trader.core.enums import OrderType, Side
from trader.execution.idempotency import OrderRepository, submit_idempotent
from trader.state.db import connect
from trader.state.migrate import run_migrations

CID = "fuzz-cid-1"
# ok: lands normally. timeout_landed: reaches the broker but the response is lost.
# timeout_lost: fails before reaching the broker. crash: restart (durable DB survives).
_ACTIONS = ["ok", "timeout_landed", "timeout_lost", "crash"]
_LANDING = {"ok", "timeout_landed"}


def _order() -> Order:
    return Order(CID, "s1", "AAPL", Side.BUY, 10, OrderType.MARKET)


def _landed(broker: FakeBroker) -> int:
    return sum(1 for f in broker._fills.values() if f.client_order_id == CID)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(_ACTIONS), min_size=1, max_size=12))
def test_at_most_once_under_interleavings(actions: list[str]) -> None:
    conn = connect(":memory:")  # fresh durable state per example
    run_migrations(conn)
    broker = FakeBroker()
    repo = OrderRepository(conn)
    order = _order()
    reconcile = lambda o: broker.find_by_client_id(o.client_order_id)  # noqa: E731

    for action in actions:
        if action == "crash":
            repo = OrderRepository(conn)  # restart: the durable orders table survives
            continue
        broker.fail_next_submit = action in ("timeout_landed", "timeout_lost")
        broker.record_on_timeout = action == "timeout_landed"
        with contextlib.suppress(Exception):  # a failed attempt is retried on the next round
            submit_idempotent(broker, repo, order, reconcile=reconcile)

    # THE property: the order is placed at the broker AT MOST ONCE, ever.
    assert _landed(broker) <= 1
    # And if any attempt could have landed, it landed EXACTLY once (never zero, never two).
    if any(a in _LANDING for a in actions):
        assert _landed(broker) == 1
