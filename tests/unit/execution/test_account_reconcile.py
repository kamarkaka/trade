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


def _run(broker: FakeBroker, repo: OrderRepository, attribution: AttributionLedger, reconcile=None):  # type: ignore[no-untyped-def]
    return reconcile_account(
        broker=broker,
        repo=repo,
        attribution=attribution,
        reconcile_order=reconcile or in_memory_reconciler(broker.find_by_client_id),
        poll_policy=PollPolicy(timeout_seconds=0, post_cancel_polls=1),
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
