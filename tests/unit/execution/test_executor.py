"""DurableOrderExecutor (LR5): place at most once -> poll to terminal -> complete atomically
(terminal status + fill row + attribution, exactly once); anything unresolved is left for
reconciliation with nothing attributed."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from fakes import FakeBroker
from trader.core import Fill, Order, OrderNotPlacedError
from trader.core.enums import OrderStatus, OrderType, Side
from trader.execution.executor import (
    DurableOrderExecutor,
    ExecutionHaltedError,
    OrderUnresolvedError,
)
from trader.execution.idempotency import (
    NOT_PLACED,
    PLACED,
    UNKNOWN,
    OrderOutcomeUnknownError,
    OrderRecord,
    OrderRepository,
    ReconcileResult,
)
from trader.execution.poller import OrderStatusUnavailableError, PollPolicy
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations


def _inconclusive(record: OrderRecord) -> ReconcileResult:
    return ReconcileResult.inconclusive("n/a")


class _CancellableBroker(FakeBroker):
    """A cancel turns the order's latest status into CANCELED (keeping what filled)."""

    def cancel_order(self, broker_order_id: str) -> None:
        super().cancel_order(broker_order_id)
        fill = self._fills[broker_order_id]
        self._fills[broker_order_id] = Fill(
            fill.client_order_id,
            fill.broker_order_id,
            fill.symbol,
            fill.quantity,
            fill.price,
            fill.fees,
            fill.ts,
            OrderStatus.CANCELED,
        )


def _setup(tmp_path: Path, broker: FakeBroker | None = None):  # type: ignore[no-untyped-def]
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    repo = OrderRepository(conn)
    attribution = AttributionLedger(conn)
    broker = broker or _CancellableBroker()
    executor = DurableOrderExecutor(
        broker=broker,
        repo=repo,
        attribution=attribution,
        reconcile=_inconclusive,
        poll_policy=PollPolicy(timeout_seconds=0, post_cancel_polls=1),
        sleep=lambda _s: None,
    )
    return executor, broker, repo, attribution, conn


def _order(cid: str = "c1", qty: int = 10, side: Side = Side.BUY) -> Order:
    return Order(cid, "s1", "AAPL", side, qty, OrderType.MARKET)


def _fill_rows(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
    rows = conn.execute(
        "SELECT client_order_id, broker_order_id, quantity, status FROM fills ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


def _attributed(attribution: AttributionLedger) -> dict[str, int]:
    return {p.symbol: p.quantity for p in attribution.get_attributed("s1")}


def _status(repo: OrderRepository, cid: str = "c1") -> tuple[str, str | None]:
    record = repo.get(cid)
    assert record is not None
    return record.status, record.broker_order_id


def test_filled_order_is_completed_atomically(tmp_path: Path) -> None:
    executor, _, repo, attribution, conn = _setup(tmp_path)
    fill = executor.execute(_order())
    assert fill.status is OrderStatus.FILLED and fill.quantity == 10
    assert _status(repo) == ("FILLED", "b-1")
    assert _fill_rows(conn) == [("c1", "b-1", 10, "FILLED")]
    assert _attributed(attribution) == {"AAPL": 10}


def test_partial_then_cancel_attributes_only_the_filled_shares(tmp_path: Path) -> None:
    executor, broker, repo, attribution, conn = _setup(tmp_path)
    broker.fill_quantity = 4  # PARTIAL_FILL; the remainder is cancelled at the deadline
    fill = executor.execute(_order())
    assert fill.status is OrderStatus.CANCELED and fill.quantity == 4
    assert broker.cancelled == ["b-1"]
    assert _status(repo) == ("CANCELED", "b-1")
    assert _fill_rows(conn) == [("c1", "b-1", 4, "CANCELED")]
    assert _attributed(attribution) == {"AAPL": 4}


def test_cancelled_with_nothing_filled_writes_no_fill_row(tmp_path: Path) -> None:
    executor, broker, repo, attribution, conn = _setup(tmp_path)
    broker.fill_quantity = 0
    fill = executor.execute(_order())
    assert fill.status is OrderStatus.CANCELED and fill.quantity == 0
    assert _status(repo)[0] == "CANCELED"
    assert _fill_rows(conn) == [] and _attributed(attribution) == {}


def test_definite_rejection_propagates_and_is_recorded(tmp_path: Path) -> None:
    executor, broker, repo, attribution, conn = _setup(tmp_path)
    broker.reject_next_submit = True
    with pytest.raises(OrderNotPlacedError):
        executor.execute(_order())
    assert _status(repo) == (NOT_PLACED, None)
    assert _fill_rows(conn) == [] and _attributed(attribution) == {}


def test_unknown_outcome_propagates_and_is_recorded(tmp_path: Path) -> None:
    executor, broker, repo, attribution, _ = _setup(tmp_path)
    broker.fail_next_submit = True
    with pytest.raises(OrderOutcomeUnknownError):
        executor.execute(_order())
    assert _status(repo) == (UNKNOWN, None) and _attributed(attribution) == {}


def test_still_working_after_the_cancel_is_left_unresolved(tmp_path: Path) -> None:
    executor, broker, repo, attribution, conn = _setup(tmp_path, FakeBroker())  # cancel no-op
    broker.fill_quantity = 4
    with pytest.raises(OrderUnresolvedError) as excinfo:
        executor.execute(_order())
    assert excinfo.value.last_fill is not None and excinfo.value.last_fill.quantity == 4
    assert _status(repo) == (PLACED, "b-1")  # non-terminal, broker id kept for reconcile
    assert _fill_rows(conn) == [] and _attributed(attribution) == {}  # nothing trusted


def test_unreadable_status_is_left_unresolved(tmp_path: Path) -> None:
    class _Unreadable(_CancellableBroker):
        def get_order(self, broker_order_id: str) -> Fill:
            raise PermissionError("safe mode")

    executor, _, repo, attribution, _ = _setup(tmp_path, _Unreadable())
    with pytest.raises(OrderUnresolvedError) as excinfo:
        executor.execute(_order())
    assert isinstance(excinfo.value.__cause__, OrderStatusUnavailableError)
    assert _status(repo) == (PLACED, "b-1") and _attributed(attribution) == {}


def test_fill_carries_our_client_order_id_even_if_the_broker_does_not(tmp_path: Path) -> None:
    class _NoCid(_CancellableBroker):
        def get_order(self, broker_order_id: str) -> Fill:
            fill = super().get_order(broker_order_id)
            return Fill("", fill.broker_order_id, fill.symbol, fill.quantity, fill.price,
                        fill.fees, fill.ts, fill.status)  # fmt: skip

    executor, _, _, _, conn = _setup(tmp_path, _NoCid())
    assert executor.execute(_order()).client_order_id == "c1"
    assert _fill_rows(conn)[0][0] == "c1"


def test_completion_is_exactly_once(tmp_path: Path) -> None:
    executor, _, repo, attribution, conn = _setup(tmp_path)
    fill = executor.execute(_order())
    record = repo.get("c1")
    assert record is not None
    again = repo.complete(record, fill, lambda: attribution.apply(fill, "s1", Side.BUY))
    assert again is False  # already terminal: no second fill row, no second attribution
    assert len(_fill_rows(conn)) == 1 and _attributed(attribution) == {"AAPL": 10}


def test_completion_rolls_back_when_attribution_fails(tmp_path: Path) -> None:
    _, broker, repo, _, conn = _setup(tmp_path)
    order = _order()
    repo._write_pending(order)
    repo.record_placed("c1", broker.submit_order(order))
    record = repo.get("c1")
    assert record is not None

    def _boom() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        repo.complete(record, broker.get_order("b-1"), _boom)
    assert _status(repo) == (PLACED, "b-1") and _fill_rows(conn) == []  # all or nothing
    assert not conn.in_transaction


def test_completion_rejects_a_fill_for_another_broker_order(tmp_path: Path) -> None:
    _, broker, repo, _, conn = _setup(tmp_path)
    order = _order()
    repo._write_pending(order)
    repo.record_placed("c1", "b-1")
    record = repo.get("c1")
    assert record is not None
    other = Fill("c1", "b-999", "AAPL", 10, Decimal("100"), Decimal("0"), broker.ts,
                 OrderStatus.FILLED)  # fmt: skip
    with pytest.raises(ValueError, match="does not belong"):
        repo.complete(record, other, lambda: None)
    assert _fill_rows(conn) == [] and not conn.in_transaction


def test_completion_requires_a_terminal_fill(tmp_path: Path) -> None:
    _, broker, repo, _, _ = _setup(tmp_path)
    repo._write_pending(_order())
    record = repo.get("c1")
    assert record is not None
    working = Fill("c1", "b-1", "AAPL", 0, Decimal("0"), Decimal("0"), broker.ts,
                   OrderStatus.WORKING)  # fmt: skip
    with pytest.raises(ValueError, match="WORKING"):
        repo.complete(record, working, lambda: None)


def test_orders_and_attribution_must_share_a_connection(tmp_path: Path) -> None:
    conn_a = connect(tmp_path / "a.sqlite")
    conn_b = connect(tmp_path / "b.sqlite")
    for conn in (conn_a, conn_b):
        run_migrations(conn)
    with pytest.raises(ValueError, match="one database connection"):
        DurableOrderExecutor(
            broker=FakeBroker(),
            repo=OrderRepository(conn_a),
            attribution=AttributionLedger(conn_b),
            reconcile=_inconclusive,
            poll_policy=PollPolicy(timeout_seconds=0),
        )


def test_repository_lookups(tmp_path: Path) -> None:
    executor, _, repo, _, _ = _setup(tmp_path)
    executor.execute(_order("c1"))
    executor.execute(_order("c2"))
    assert repo.client_id_for("b-2") == "c2" and repo.client_id_for("nope") is None
    assert repo.bound_broker_ids() == {"b-1", "b-2"}


# --- review follow-ups ------------------------------------------------------------ #


def test_two_paper_processes_on_one_database_never_collide(tmp_path: Path) -> None:
    # Every paper process restarts its in-memory SimBroker; with a per-process id prefix
    # its orders never collide with the durable rows of an earlier process.
    from fakes import FakeClock, FakeMarketDataProvider
    from trader.broker import SimBroker
    from trader.core import Quote
    from trader.execution.executor import in_memory_reconciler

    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    price = Decimal("100")
    ts = FakeBroker().ts
    data = FakeMarketDataProvider(quotes={"AAPL": [Quote("AAPL", ts, price, price, price, 1000)]})
    repo, attribution = OrderRepository(conn), AttributionLedger(conn)
    for run, prefix in enumerate(("SIM-aaaa", "SIM-bbbb")):
        sim = SimBroker(data, FakeClock(ts), starting_cash=Decimal("100000"), id_prefix=prefix)
        executor = DurableOrderExecutor(
            broker=sim,
            repo=repo,
            attribution=attribution,
            reconcile=in_memory_reconciler(sim.find_by_client_id),
            poll_policy=PollPolicy(timeout_seconds=0),
        )
        fill = executor.execute(_order(f"run{run}", qty=5))
        assert fill.status is OrderStatus.FILLED and fill.broker_order_id == f"{prefix}-1"
    assert _attributed(attribution) == {"AAPL": 10}  # both processes' fills attributed


def test_a_transient_completion_failure_is_retried(tmp_path: Path) -> None:
    executor, _, repo, attribution, conn = _setup(tmp_path)
    real_complete = repo.complete
    calls = {"n": 0}

    def flaky(*args: object, **kwargs: object) -> bool:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_complete(*args, **kwargs)  # type: ignore[arg-type]

    repo.complete = flaky  # type: ignore[method-assign]
    executor.execute(_order())
    assert calls["n"] == 3 and _attributed(attribution) == {"AAPL": 10}
    assert _status(repo)[0] == "FILLED" and len(_fill_rows(conn)) == 1


def test_a_fill_that_cannot_be_recorded_is_unresolved_with_its_details(tmp_path: Path) -> None:
    executor, _, repo, attribution, _ = _setup(tmp_path)

    def stuck(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    repo.complete = stuck  # type: ignore[method-assign]
    with pytest.raises(OrderUnresolvedError, match="FILLED 10 filled but not recorded") as ei:
        executor.execute(_order())
    assert ei.value.last_fill is not None and ei.value.last_fill.quantity == 10
    assert _status(repo) == (PLACED, "b-1") and _attributed(attribution) == {}


def test_a_fill_that_contradicts_the_order_is_never_attributed(tmp_path: Path) -> None:
    class _WrongSymbol(_CancellableBroker):
        def get_order(self, broker_order_id: str) -> Fill:
            f = super().get_order(broker_order_id)
            return Fill(f.client_order_id, f.broker_order_id, "TSLA", f.quantity, f.price,
                        f.fees, f.ts, f.status)  # fmt: skip

    executor, _, _, attribution, conn = _setup(tmp_path, _WrongSymbol())
    with pytest.raises(OrderUnresolvedError, match="fill rejected: fill symbol"):
        executor.execute(_order())
    assert _attributed(attribution) == {} and _fill_rows(conn) == []


def test_completion_rejects_more_shares_than_ordered(tmp_path: Path) -> None:
    _, broker, repo, _, _ = _setup(tmp_path)
    repo._write_pending(_order(qty=10))
    repo.record_placed("c1", "b-1")
    record = repo.get("c1")
    assert record is not None
    too_many = Fill("c1", "b-1", "AAPL", 11, Decimal("100"), Decimal("0"), broker.ts,
                    OrderStatus.FILLED)  # fmt: skip
    with pytest.raises(ValueError, match="filled 11 > ordered 10"):
        repo.complete(record, too_many, lambda: None)


def test_an_unresolved_error_reports_whether_the_cancel_went_through(tmp_path: Path) -> None:
    broker = FakeBroker()  # cancel is accepted but doesn't change the (partial) status
    broker.fill_quantity = 4
    executor, _, _, _, _ = _setup(tmp_path, broker)
    with pytest.raises(OrderUnresolvedError) as ei:
        executor.execute(_order())
    assert ei.value.cancel_attempted and ei.value.cancel_accepted


# --- the uncertainty hook (LR7) --------------------------------------------------- #


def _hooked(tmp_path: Path, broker: FakeBroker):  # type: ignore[no-untyped-def]
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    calls: list[str] = []
    executor = DurableOrderExecutor(
        broker=broker,
        repo=OrderRepository(conn),
        attribution=AttributionLedger(conn),
        reconcile=_inconclusive,
        poll_policy=PollPolicy(timeout_seconds=0, post_cancel_polls=1),
        sleep=lambda _s: None,
        on_uncertain=calls.append,
    )
    return executor, calls


def test_an_unknown_outcome_calls_the_uncertainty_hook(tmp_path: Path) -> None:
    broker = _CancellableBroker()
    broker.fail_next_submit = True
    executor, calls = _hooked(tmp_path, broker)
    with pytest.raises(OrderOutcomeUnknownError):
        executor.execute(_order())
    assert len(calls) == 1 and "outcome unknown" in calls[0]


def test_an_unresolved_order_calls_the_uncertainty_hook(tmp_path: Path) -> None:
    broker = FakeBroker()  # cancel is a no-op: a partial fill stays non-terminal
    broker.fill_quantity = 4
    executor, calls = _hooked(tmp_path, broker)
    with pytest.raises(OrderUnresolvedError):
        executor.execute(_order())
    assert len(calls) == 1 and "unresolved" in calls[0]


def test_a_definite_rejection_or_a_fill_does_not_call_the_hook(tmp_path: Path) -> None:
    broker = _CancellableBroker()
    broker.reject_next_submit = True
    executor, calls = _hooked(tmp_path, broker)
    with pytest.raises(OrderNotPlacedError):
        executor.execute(_order("c1"))
    executor.execute(_order("c2"))  # filled normally
    assert calls == []


def test_a_failing_hook_halts_the_executor_until_restart(tmp_path: Path) -> None:
    # The kill switch could not be engaged: this process must send nothing more.
    broker = _CancellableBroker()
    broker.fail_next_submit = True
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)

    def boom(reason: str) -> None:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    executor = DurableOrderExecutor(
        broker=broker,
        repo=OrderRepository(conn),
        attribution=AttributionLedger(conn),
        reconcile=_inconclusive,
        poll_policy=PollPolicy(timeout_seconds=0),
        on_uncertain=boom,
    )
    with pytest.raises(ExecutionHaltedError, match="could not engage the kill switch") as ei:
        executor.execute(_order("c1"))
    assert isinstance(ei.value.__cause__, OrderOutcomeUnknownError)  # the original, chained
    sent = len(broker.submitted)
    with pytest.raises(ExecutionHaltedError):
        executor.execute(_order("c2"))
    assert len(broker.submitted) == sent  # refused before any send
    assert OrderRepository(conn).get("c2") is None  # not even a write-ahead row


def test_a_fill_that_cannot_be_recorded_calls_the_hook(tmp_path: Path) -> None:
    executor, calls = _hooked(tmp_path, _CancellableBroker())
    repo = executor._repo

    def stuck(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    repo.complete = stuck  # type: ignore[method-assign]
    with pytest.raises(OrderUnresolvedError):
        executor.execute(_order())
    assert len(calls) == 1 and "not recorded" in calls[0]


def test_any_other_failure_after_placement_calls_the_hook(tmp_path: Path) -> None:
    # Not an unknown/unresolved outcome as such, but the order is at the broker and its fill
    # is not recorded: just as uncertain.
    executor, calls = _hooked(tmp_path, _CancellableBroker())

    def broken(*args: object, **kwargs: object) -> bool:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: fills.broker_order_id")

    executor._repo.complete = broken  # type: ignore[method-assign]
    with pytest.raises(sqlite3.IntegrityError):
        executor.execute(_order())
    assert len(calls) == 1 and "IntegrityError" in calls[0]
