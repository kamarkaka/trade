"""Idempotent order placement: write-ahead ordering, retry/crash safety, and
reconcile-before-resend so an order's outcome is never doubled (M5.3)."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeBroker
from trader.core import Fill, Order
from trader.core.enums import OrderType, Side
from trader.execution.idempotency import OrderRepository, submit_idempotent
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _repo(tmp_path: Path) -> tuple[OrderRepository, object]:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return OrderRepository(conn, now=lambda: NOW), conn


def _order(cid: str = "c1", qty: int = 10) -> Order:
    return Order(cid, "s1", "AAPL", Side.BUY, qty, OrderType.MARKET)


def _reconcile(broker: FakeBroker):
    return lambda order: broker.find_by_client_id(order.client_order_id)


def _landed(broker: FakeBroker, cid: str) -> int:
    return sum(1 for f in broker._fills.values() if f.client_order_id == cid)


def test_pending_persisted_before_submit(tmp_path: Path) -> None:
    repo, conn = _repo(tmp_path)

    class _Probe(FakeBroker):
        status_at_submit: str | None = "<unset>"

        def submit_order(self, order: Order) -> str:
            row = conn.execute(  # type: ignore[attr-defined]
                "SELECT status FROM orders WHERE client_order_id = ?", (order.client_order_id,)
            ).fetchone()
            self.status_at_submit = row[0] if row else None
            return super().submit_order(order)

    broker = _Probe()
    submit_idempotent(broker, repo, _order(), reconcile=_reconcile(broker))
    assert broker.status_at_submit == "pending"  # write-ahead happened BEFORE the submit


def test_retry_reuses_client_id_no_double_fill(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    order = _order()
    submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))  # retry same id
    assert _landed(broker, "c1") == 1  # placed exactly once
    assert len(broker.submitted) == 1  # second call polled, never re-submitted


def test_already_submitted_polls_not_resubmits(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    order = _order()
    submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    fill = submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    assert fill.client_order_id == "c1"  # polled the existing order
    assert len(broker.submitted) == 1


def test_lost_response_reconcile_before_resend(tmp_path: Path) -> None:
    # The order lands at the broker but the response is lost (timeout). The retry must
    # RECONCILE and adopt it, never place a second order.
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.fail_next_submit = True
    broker.record_on_timeout = True  # request reached the broker; response lost
    order = _order()
    try:
        submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
        raise AssertionError("expected the timeout to propagate")
    except TimeoutError:
        pass
    assert _landed(broker, "c1") == 1  # it did land
    # retry: must adopt the landed order, not double it
    fill = submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    assert _landed(broker, "c1") == 1  # still exactly one
    assert fill.status.value in ("FILLED", "PARTIAL_FILL", "WORKING")


def test_failed_submit_resends_only_when_not_landed(tmp_path: Path) -> None:
    # The order did NOT land (failure before reaching the broker). The retry, finding no
    # existing order via reconcile, safely resends -> exactly one order.
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.fail_next_submit = True
    broker.record_on_timeout = False  # never reached the broker
    order = _order()
    try:
        submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
        raise AssertionError("expected the failure to propagate")
    except TimeoutError:
        pass
    assert _landed(broker, "c1") == 0  # nothing landed
    submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))  # safe resend
    assert _landed(broker, "c1") == 1


def test_crash_after_landing_recovers_without_double(tmp_path: Path) -> None:
    # Simulate a crash between landing and recording the broker id: the row is 'pending'
    # with no broker_order_id, but the order DID land. A fresh repo (restart) must reconcile.
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    order = _order()
    broker = FakeBroker()
    # write-ahead pending, then the order lands but the process "crashes" before mark_submitted
    OrderRepository(conn, now=lambda: NOW).write_pending(order)
    broker.submit_order(order)  # landed (1)
    # restart: brand-new repo over the same durable DB
    repo2 = OrderRepository(conn, now=lambda: NOW)
    fill = submit_idempotent(broker, repo2, order, reconcile=_reconcile(broker))
    assert _landed(broker, "c1") == 1  # reconciled + adopted, not doubled
    assert isinstance(fill, Fill) and fill.client_order_id == "c1"
    _ = Decimal  # keep import used
