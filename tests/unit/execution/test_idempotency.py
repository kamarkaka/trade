"""Idempotent order placement: write-ahead ordering, broker-id capture at submit, outcome
classification (placed / not placed / unknown), and reconcile-before-resend so an order's
outcome is never doubled (M5.3 + LR3)."""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fakes import FakeBroker
from trader.core import Fill, Order, OrderNotPlacedError
from trader.core.enums import OrderStatus, OrderType, Side
from trader.execution.idempotency import (
    NOT_PLACED,
    PENDING,
    UNKNOWN,
    OrderOutcomeUnknownError,
    OrderRecord,
    OrderRepository,
    ReconcileOutcome,
    ReconcileResult,
    place_idempotent,
    submit_idempotent,
)
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _repo(tmp_path: Path) -> tuple[OrderRepository, sqlite3.Connection]:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return OrderRepository(conn, now=lambda: NOW), conn


def _order(cid: str = "c1", qty: int = 10) -> Order:
    return Order(cid, "s1", "AAPL", Side.BUY, qty, OrderType.MARKET)


def _reconcile(broker: FakeBroker):  # type: ignore[no-untyped-def]
    """A PERFECT (authoritative, synchronous) reconciler over the FakeBroker."""

    def reconcile(record: OrderRecord) -> ReconcileResult:
        fill = broker.find_by_client_id(record.client_order_id)
        return ReconcileResult.found(fill.broker_order_id) if fill else ReconcileResult.absent()

    return reconcile


def _inconclusive(record: OrderRecord) -> ReconcileResult:
    return ReconcileResult.inconclusive("lagging")


def _landed(broker: FakeBroker, cid: str) -> int:
    return sum(1 for f in broker._fills.values() if f.client_order_id == cid)


def _status(repo: OrderRepository, cid: str = "c1") -> tuple[str, str | None]:
    record = repo.get(cid)
    assert record is not None
    return record.status, record.broker_order_id


def test_pending_persisted_before_submit(tmp_path: Path) -> None:
    repo, conn = _repo(tmp_path)

    class _Probe(FakeBroker):
        status_at_submit: str | None = "<unset>"

        def submit_order(self, order: Order) -> str:
            row = conn.execute(
                "SELECT status FROM orders WHERE client_order_id = ?", (order.client_order_id,)
            ).fetchone()
            self.status_at_submit = row[0] if row else None
            return super().submit_order(order)

    broker = _Probe()
    submit_idempotent(broker, repo, _order(), reconcile=_reconcile(broker))
    assert broker.status_at_submit == PENDING  # write-ahead happened BEFORE the submit


def test_record_round_trips_the_full_intent(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    order = Order("c9", "s1", "MSFT", Side.SELL, 3, OrderType.LIMIT, Decimal("410.50"))
    repo.write_pending(order)
    record = repo.get("c9")
    assert record is not None and record.to_order() == order
    assert record.status == PENDING and record.broker_order_id is None
    assert record.created_at == NOW and record.updated_at == NOW


def test_broker_id_captured_before_the_first_poll(tmp_path: Path) -> None:
    # The order lands, then the status read fails: the broker id must already be durable
    # so a retry only polls — it never re-submits or falls back to intent-matching.
    repo, _ = _repo(tmp_path)

    class _PollFails(FakeBroker):
        fail_poll = True

        def get_order(self, broker_order_id: str) -> Fill:
            if self.fail_poll:
                raise TimeoutError("status read timed out")
            return super().get_order(broker_order_id)

    broker = _PollFails()
    with pytest.raises(TimeoutError):
        submit_idempotent(broker, repo, _order(), reconcile=_reconcile(broker))
    status, broker_order_id = _status(repo)
    assert status == OrderStatus.WORKING.value and broker_order_id == "b-1"
    broker.fail_poll = False
    fill = submit_idempotent(broker, repo, _order(), reconcile=_inconclusive)
    assert fill.broker_order_id == "b-1" and len(broker.submitted) == 1  # polled, not resent


def test_retry_reuses_client_id_no_double_fill(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    order = _order()
    submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    submit_idempotent(broker, repo, order, reconcile=_reconcile(broker))  # retry same id
    assert _landed(broker, "c1") == 1  # placed exactly once
    assert len(broker.submitted) == 1  # second call polled, never re-submitted


def test_definite_rejection_is_terminal_not_placed(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.reject_next_submit = True
    with pytest.raises(OrderNotPlacedError):
        place_idempotent(broker, repo, _order(), reconcile=_reconcile(broker))
    assert _status(repo) == (NOT_PLACED, None) and _landed(broker, "c1") == 0
    # A retry of a definitely-not-placed order may be sent (no prior order can exist).
    place_idempotent(broker, repo, _order(), reconcile=_inconclusive)
    assert _landed(broker, "c1") == 1


def test_unknown_outcome_marks_unknown_and_never_resends(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.fail_next_submit = True
    broker.record_on_timeout = True  # the request landed; the response was lost
    with pytest.raises(OrderOutcomeUnknownError) as excinfo:
        place_idempotent(broker, repo, _order(), reconcile=_reconcile(broker))
    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert _status(repo) == (UNKNOWN, None)
    # A retry while the reconciler cannot see the order yet (lag) must REFUSE, not resend.
    with pytest.raises(OrderOutcomeUnknownError, match="inconclusive"):
        place_idempotent(broker, repo, _order(), reconcile=_inconclusive)
    assert len(broker.submitted) == 1 and _landed(broker, "c1") == 1
    assert _status(repo) == (UNKNOWN, None)


def test_reconciler_that_raises_is_inconclusive(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.fail_next_submit = True
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(broker, repo, _order(), reconcile=_reconcile(broker))

    def _boom(record: OrderRecord) -> ReconcileResult:
        raise ConnectionError("listing failed")

    with pytest.raises(OrderOutcomeUnknownError, match="ConnectionError"):
        place_idempotent(broker, repo, _order(), reconcile=_boom)
    assert len(broker.submitted) == 1  # never re-sent on an unproven lookup


def test_lost_response_reconcile_adopts_instead_of_resending(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.fail_next_submit = True
    broker.record_on_timeout = True
    order = _order()
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    broker_order_id = place_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    assert broker_order_id == "b-1" and _landed(broker, "c1") == 1  # adopted, not doubled
    assert _status(repo) == (OrderStatus.WORKING.value, "b-1")


def test_authoritative_absent_permits_resend_with_same_id(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.fail_next_submit = True  # record_on_timeout False: never reached the broker
    order = _order()
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(broker, repo, order, reconcile=_reconcile(broker))
    assert _landed(broker, "c1") == 0
    place_idempotent(broker, repo, order, reconcile=_reconcile(broker))  # ABSENT -> resend
    assert _landed(broker, "c1") == 1
    assert [o.client_order_id for o in broker.submitted] == ["c1", "c1"]  # same id reused


def test_crash_after_landing_recovers_without_double(tmp_path: Path) -> None:
    # A crash between landing and recording the broker id: the row is 'pending' with no
    # broker id but the order DID land. A fresh repo (restart) must reconcile and adopt.
    repo, conn = _repo(tmp_path)
    order = _order()
    broker = FakeBroker()
    repo.write_pending(order)
    broker.submit_order(order)  # landed (1); the process "crashes" before mark_placed
    repo2 = OrderRepository(conn, now=lambda: NOW)
    fill = submit_idempotent(broker, repo2, order, reconcile=_reconcile(broker))
    assert _landed(broker, "c1") == 1 and fill.client_order_id == "c1"


def test_reconcile_result_validation() -> None:
    assert ReconcileResult.found("b-1").outcome is ReconcileOutcome.FOUND
    with pytest.raises(ValueError, match="broker_order_id"):
        ReconcileResult(ReconcileOutcome.FOUND)
    with pytest.raises(ValueError, match="broker_order_id"):
        ReconcileResult(ReconcileOutcome.ABSENT, "b-1")
