"""Account reconciliation (LR9): every open order row is settled — resolved, polled to a
terminal status and completed atomically — before positions are trued to the broker."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fakes import FakeBroker
from trader.core import Fill, Order, Position
from trader.core.enums import OrderStatus, OrderType, Side
from trader.execution.account_reconcile import Settlement, reconcile_account, summary_lines
from trader.execution.executor import in_memory_reconciler
from trader.execution.idempotency import (
    OrderOutcomeUnknownError,
    OrderRecord,
    OrderRepository,
    ReconcileResult,
    place_idempotent,
)
from trader.execution.poller import PollPolicy
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.lease import TradingLease
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Cancellable(FakeBroker):
    def cancel_order(self, broker_order_id: str) -> None:
        super().cancel_order(broker_order_id)
        f = self._fills[broker_order_id]
        self._fills[broker_order_id] = Fill(
            f.client_order_id, f.broker_order_id, f.symbol, f.quantity, f.price, f.fees, f.ts,
            OrderStatus.CANCELED,
        )  # fmt: skip


def _setup(tmp_path: Path, broker: FakeBroker):  # type: ignore[no-untyped-def]
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return OrderRepository(conn), AttributionLedger(conn), conn


def _order(cid: str, symbol: str = "AAPL", qty: int = 10) -> Order:
    return Order(cid, "s1", symbol, Side.BUY, qty, OrderType.MARKET)


class _HeldLease:
    """Stands in for the trading lease the caller holds (see test_lease.py)."""

    held = True


def _run(broker: FakeBroker, repo: OrderRepository, attribution: AttributionLedger, reconcile=None):  # type: ignore[no-untyped-def]
    return reconcile_account(
        broker=broker,
        repo=repo,
        attribution=attribution,
        reconcile_order=reconcile or in_memory_reconciler(broker.find_by_client_id),
        poll_policy=PollPolicy(timeout_seconds=0, post_cancel_polls=1),
        lease=_HeldLease(),  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )


def _outcomes(report) -> dict[str, Settlement]:  # type: ignore[no-untyped-def]
    return {o.client_order_id: o.outcome for o in report.orders}


def test_a_placed_but_uncompleted_order_is_completed_and_attributed(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, conn = _setup(tmp_path, broker)
    place_idempotent(
        broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
    )
    broker.set_position(Position("AAPL", 10, Decimal("100"), Decimal("1000")))
    report = _run(broker, repo, attribution)  # e.g. the daemon died before completing it
    assert _outcomes(report) == {"c1": Settlement.COMPLETED}
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "FILLED"
    assert {p.symbol: p.quantity for p in attribution.get_attributed("s1")} == {"AAPL": 10}
    assert report.positions.is_clean and report.is_clean  # attributed BEFORE trueing positions


def test_an_unknown_order_that_landed_is_adopted_then_completed(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    broker.fail_next_submit = True
    broker.record_on_timeout = True  # it landed; the answer was lost
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(
            broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
        )
    broker.set_position(Position("AAPL", 10, Decimal("100"), Decimal("1000")))
    report = _run(broker, repo, attribution)
    assert _outcomes(report) == {"c1": Settlement.COMPLETED} and report.is_clean


def test_an_unknown_order_confirmed_absent_is_marked_not_placed(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    broker.fail_next_submit = True  # never reached the broker
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(
            broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
        )
    report = _run(broker, repo, attribution)
    assert _outcomes(report) == {"c1": Settlement.NOT_PLACED} and report.is_clean


def test_an_interrupted_send_waits_out_the_window(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    repo._write_pending(_order("c1"))  # the sender died mid-send
    report = _run(broker, repo, attribution)
    (settlement,) = report.orders
    assert settlement.outcome is Settlement.UNRESOLVED and settlement.code == "re_anchored"
    assert not report.is_clean and report.retry_later
    assert any("run again after the consistency window" in line for line in summary_lines(report))


def test_an_ambiguous_order_needs_attention(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    repo._write_pending(_order("c1"))
    repo.mark_unknown_after_send("c1")

    def ambiguous(record: OrderRecord) -> ReconcileResult:
        return ReconcileResult.inconclusive("two candidates", "ambiguous")

    report = _run(broker, repo, attribution, ambiguous)
    assert report.orders[0].code == "ambiguous" and not report.retry_later
    assert summary_lines(report)[-1] == "result: NOT CLEAN - needs attention"


def test_an_unreadable_status_leaves_the_order_unresolved(tmp_path: Path) -> None:
    class _Unreadable(_Cancellable):
        def get_order(self, broker_order_id: str) -> Fill:
            raise PermissionError("safe mode")

    broker = _Unreadable()
    repo, attribution, conn = _setup(tmp_path, broker)
    place_idempotent(
        broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
    )
    report = _run(broker, repo, attribution)
    assert report.orders[0].code == "status_unavailable"
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "WORKING"
    assert broker.cancelled == []  # nothing known about it: never cancelled blind


def test_position_divergence_is_reported_and_parked(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    broker.set_position(Position("TSLA", 5, Decimal("200"), Decimal("1000")))  # bought by hand
    report = _run(broker, repo, attribution)
    assert report.orders == () and not report.positions.is_clean and not report.is_clean
    lines = summary_lines(report)
    assert any("TSLA: broker 5" in line for line in lines)
    assert {p.symbol: p.quantity for p in attribution.get_attributed("unknown")} == {"TSLA": 5}


def test_a_clean_account_reports_clean(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    report = _run(broker, repo, attribution)
    assert report.is_clean and summary_lines(report)[-1] == "result: CLEAN"
    _ = (sqlite3, timedelta)


# --- review follow-ups ------------------------------------------------------------- #


def _bound(repo: OrderRepository, broker: FakeBroker, fill: Fill, cid: str = "c1") -> None:
    """A row bound (e.g. by hand) to the broker order ``fill`` describes."""
    repo._write_pending(_order(cid))
    repo.record_placed(cid, fill.broker_order_id)
    broker._fills[fill.broker_order_id] = fill


def _working(broker_order_id: str, symbol: str = "AAPL", qty: int = 0) -> Fill:
    return Fill("", broker_order_id, symbol, qty, Decimal("100"), Decimal("0"), NOW,
                OrderStatus.WORKING)  # fmt: skip


def test_reconciliation_needs_the_trading_lease_held(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    with pytest.raises(RuntimeError, match="trading lease"):
        reconcile_account(
            broker=broker,
            repo=repo,
            attribution=attribution,
            reconcile_order=in_memory_reconciler(broker.find_by_client_id),
            poll_policy=PollPolicy(timeout_seconds=0),
            lease=TradingLease(tmp_path / "s.sqlite"),  # not acquired
        )


@pytest.mark.parametrize(
    ("fill", "problem"),
    [
        (_working("b-9", symbol="TSLA"), "symbol 'TSLA', ordered 'AAPL'"),  # someone else's
        (_working("b-9", qty=25), "filled 25, ordered 10"),
    ],
    ids=["other-symbol", "over-filled"],
)
def test_a_bound_order_that_cannot_be_the_rows_is_left_untouched(
    tmp_path: Path, fill: Fill, problem: str
) -> None:
    broker = _Cancellable()
    repo, attribution, conn = _setup(tmp_path, broker)
    _bound(repo, broker, fill)
    report = _run(broker, repo, attribution)
    (settlement,) = report.orders
    assert settlement.code == "bound_order_mismatch" and problem in settlement.detail
    assert broker.cancelled == []  # never polled, so never cancelled
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "WORKING"
    assert attribution.get_attributed("s1") == []


def test_transient_failures_reading_a_bound_order_are_retried(tmp_path: Path) -> None:
    class _Flaky(_Cancellable):
        failures = 2

        def get_order(self, broker_order_id: str) -> Fill:
            if self.failures:
                self.failures -= 1
                raise TimeoutError("slow")
            return super().get_order(broker_order_id)

    broker = _Flaky()
    repo, attribution, _ = _setup(tmp_path, broker)
    place_idempotent(
        broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
    )
    broker.set_position(Position("AAPL", 10, Decimal("100"), Decimal("1000")))
    assert _outcomes(_run(broker, repo, attribution)) == {"c1": Settlement.COMPLETED}
    broker.failures = 3  # every read fails: unresolved, untouched
    _bound(repo, broker, _working("b-9"), cid="c2")
    report = _run(broker, repo, attribution)
    assert report.orders[0].code == "status_unavailable" and broker.cancelled == []


def test_a_fill_the_repository_refuses_leaves_the_order_unresolved(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    place_idempotent(
        broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
    )

    def refuse(*args: object) -> bool:
        raise ValueError("fill symbol 'TSLA' != order symbol 'AAPL'")

    repo.complete = refuse  # type: ignore[method-assign]
    (settlement,) = _run(broker, repo, attribution).orders
    assert settlement.code == "fill_refused" and "TSLA" in settlement.detail
    assert attribution.get_attributed("s1") == []


def test_an_order_completed_meanwhile_is_reported_as_already_completed(tmp_path: Path) -> None:
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    place_idempotent(
        broker, repo, _order("c1"), reconcile=in_memory_reconciler(broker.find_by_client_id)
    )
    repo.complete = lambda *args: False  # type: ignore[method-assign]
    (settlement,) = _run(broker, repo, attribution).orders
    assert (settlement.outcome, settlement.detail) == (Settlement.COMPLETED, "already completed")


def test_an_interrupted_send_is_re_anchored_then_adopted_after_the_window(tmp_path: Path) -> None:
    # The daemon died mid-send, but the order landed. Schwab's reconciler answers NOT_SETTLED
    # for the pending row (resolve re-anchors it), WINDOW_OPEN until the consistency window
    # has passed, then FOUND — and the order is adopted, polled and completed.
    broker = _Cancellable()
    repo, attribution, _ = _setup(tmp_path, broker)
    order = _order("c1")
    repo._write_pending(order)
    broker_order_id = broker.submit_order(order)
    broker.set_position(Position("AAPL", 10, Decimal("100"), Decimal("1000")))
    answers = iter(
        [
            ReconcileResult.inconclusive("inside the consistency window", "window_open"),
            ReconcileResult.found(broker_order_id, "unique intent match"),
        ]
    )
    seen: list[str] = []

    def schwab_like(record: OrderRecord) -> ReconcileResult:
        seen.append(record.status)
        if record.status != "unknown":
            return ReconcileResult.inconclusive("no settled send window yet", "not_settled")
        return next(answers)

    first = _run(broker, repo, attribution, schwab_like)
    assert first.orders[0].code == "re_anchored" and first.retry_later
    second = _run(broker, repo, attribution, schwab_like)
    assert second.orders[0].code == "window_open" and second.retry_later
    third = _run(broker, repo, attribution, schwab_like)
    assert _outcomes(third) == {"c1": Settlement.COMPLETED} and third.is_clean
    assert seen == ["pending", "unknown", "unknown"]
    assert {p.symbol: p.quantity for p in attribution.get_attributed("s1")} == {"AAPL": 10}
