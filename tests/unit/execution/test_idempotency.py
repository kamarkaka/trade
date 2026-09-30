"""Idempotent order placement (M5.3 + LR3): exactly one send per client_order_id, durable
write-ahead, broker-id capture at submit, outcome classification (placed / not placed /
unknown), and resolution that never re-sends — with compare-and-swap state transitions."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fakes import FakeBroker
from trader.core import Fill, Order, OrderNotPlacedError
from trader.core.enums import OrderType, Side
from trader.execution.idempotency import (
    NOT_PLACED,
    PENDING,
    PLACED,
    UNKNOWN,
    OrderOutcomeUnknownError,
    OrderRecord,
    OrderRepository,
    ReconcileOutcome,
    ReconcileResult,
    ResolveOutcome,
    place_idempotent,
    resolve,
    submit_idempotent,
)
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return conn


def _repo(
    tmp_path: Path, clock: _Clock | None = None
) -> tuple[OrderRepository, sqlite3.Connection]:
    conn = _conn(tmp_path)
    return OrderRepository(conn, now=clock or (lambda: NOW)), conn


def _order(cid: str = "c1", qty: int = 10) -> Order:
    return Order(cid, "s1", "AAPL", Side.BUY, qty, OrderType.MARKET)


def _perfect(broker: FakeBroker):  # type: ignore[no-untyped-def]
    """An authoritative, synchronous reconciler over the FakeBroker."""

    def reconcile(record: OrderRecord) -> ReconcileResult:
        fill = broker.find_by_client_id(record.client_order_id)
        return ReconcileResult.found(fill.broker_order_id) if fill else ReconcileResult.absent()

    return reconcile


def _inconclusive(record: OrderRecord) -> ReconcileResult:
    return ReconcileResult.inconclusive("lagging")


def _absent(record: OrderRecord) -> ReconcileResult:
    return ReconcileResult.absent("window elapsed")


def _landed(broker: FakeBroker, cid: str = "c1") -> int:
    return sum(1 for f in broker._fills.values() if f.client_order_id == cid)


def _row(repo: OrderRepository, cid: str = "c1") -> OrderRecord:
    record = repo.get(cid)
    assert record is not None
    return record


def _unknown_row(tmp_path: Path, *, landed: bool, clock: _Clock | None = None):  # type: ignore[no-untyped-def]
    """A row whose single send timed out (landed or not) -> status unknown."""
    repo, conn = _repo(tmp_path, clock)
    broker = FakeBroker()
    broker.fail_next_submit = True
    broker.record_on_timeout = landed
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    return repo, conn, broker


# --- the one send ---------------------------------------------------------------- #


def test_pending_is_committed_before_submit(tmp_path: Path) -> None:
    repo, conn = _repo(tmp_path)

    class _Probe(FakeBroker):
        seen: tuple[str | None, bool] = ("<unset>", True)

        def submit_order(self, order: Order) -> str:
            row = conn.execute(
                "SELECT status FROM orders WHERE client_order_id = ?", (order.client_order_id,)
            ).fetchone()
            self.seen = (row[0] if row else None, conn.in_transaction)
            return super().submit_order(order)

    broker = _Probe()
    submit_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert broker.seen == (PENDING, False)  # durable (committed) BEFORE the network call


def test_refuses_to_send_inside_an_open_transaction(tmp_path: Path) -> None:
    repo, conn = _repo(tmp_path)
    broker = FakeBroker()
    conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="open transaction"):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    conn.execute("ROLLBACK")
    assert broker.submitted == [] and repo.get("c1") is None


def test_record_round_trips_the_full_intent(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    order = Order("c9", "s1", "MSFT", Side.SELL, 3, OrderType.LIMIT, Decimal("410.50"))
    repo._write_pending(order)
    record = _row(repo, "c9")
    assert record.to_order() == order
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
        submit_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert (_row(repo).status, _row(repo).broker_order_id) == (PLACED, "b-1")
    broker.fail_poll = False
    fill = submit_idempotent(broker, repo, _order(), reconcile=_inconclusive)
    assert fill.broker_order_id == "b-1" and len(broker.submitted) == 1


def test_exactly_one_send_per_client_order_id(tmp_path: Path) -> None:
    # Even an authoritative ABSENT after a lost-before-landing send never re-sends the id:
    # the row becomes not_placed and the intent needs a fresh client_order_id.
    repo, _, broker = _unknown_row(tmp_path, landed=False)
    with pytest.raises(OrderNotPlacedError):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert len(broker.submitted) == 1 and _landed(broker) == 0
    assert _row(repo).status == NOT_PLACED


def test_definite_rejection_is_terminal_and_never_resent(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    broker.reject_next_submit = True
    with pytest.raises(OrderNotPlacedError):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert (_row(repo).status, _row(repo).broker_order_id) == (NOT_PLACED, None)
    with pytest.raises(OrderNotPlacedError):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert len(broker.submitted) == 1  # the retry resolved; it did not send


def test_unknown_outcome_is_recorded_after_the_send(tmp_path: Path) -> None:
    clock = _Clock()
    repo, _ = _repo(tmp_path, clock)

    class _SlowTimeout(FakeBroker):
        def submit_order(self, order: Order) -> str:
            clock.advance(30)  # the request hangs, then times out
            return super().submit_order(order)

    broker = _SlowTimeout()
    broker.fail_next_submit = True
    broker.record_on_timeout = True
    with pytest.raises(OrderOutcomeUnknownError) as excinfo:
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert isinstance(excinfo.value.__cause__, TimeoutError)
    record = _row(repo)
    assert record.status == UNKNOWN
    assert record.updated_at == NOW + timedelta(seconds=30)  # anchored AFTER the send


def test_missing_broker_id_is_an_unknown_outcome(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)

    class _NoId(FakeBroker):
        def submit_order(self, order: Order) -> str:
            super().submit_order(order)
            return ""  # e.g. a 2xx without a Location header

    broker = _NoId()
    with pytest.raises(OrderOutcomeUnknownError, match="no order id"):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert _row(repo).status == UNKNOWN


def test_failure_to_record_the_id_is_unknown_and_names_the_id(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)

    class _BrokenRepo(OrderRepository):
        def record_placed(self, client_order_id: str, broker_order_id: str) -> str | None:
            raise sqlite3.OperationalError("database is locked")

    broken = _BrokenRepo(repo._conn, now=lambda: NOW)
    broker = FakeBroker()
    with pytest.raises(OrderOutcomeUnknownError, match="placed as b-1") as excinfo:
        place_idempotent(broker, broken, _order(), reconcile=_perfect(broker))
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)


def test_placement_overrides_a_premature_not_placed(tmp_path: Path) -> None:
    # A concurrent resolve (wrongly) marked the row not_placed while the send was in flight:
    # the broker's acceptance is ground truth and must be recorded.
    repo, conn = _repo(tmp_path)

    class _Racy(FakeBroker):
        def submit_order(self, order: Order) -> str:
            conn.execute(
                "UPDATE orders SET status = ? WHERE client_order_id = ?",
                (NOT_PLACED, order.client_order_id),
            )
            return super().submit_order(order)

    broker = _Racy()
    assert place_idempotent(broker, repo, _order(), reconcile=_perfect(broker)) == "b-1"
    assert (_row(repo).status, _row(repo).broker_order_id) == (PLACED, "b-1")


def test_client_order_id_reused_for_a_different_intent_is_rejected(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    place_idempotent(broker, repo, _order(qty=10), reconcile=_perfect(broker))
    with pytest.raises(ValueError, match="different order"):
        place_idempotent(broker, repo, _order(qty=99), reconcile=_perfect(broker))
    assert len(broker.submitted) == 1


# --- resolution (never sends) ------------------------------------------------------ #


def test_found_adopts_the_landed_order(tmp_path: Path) -> None:
    repo, _, broker = _unknown_row(tmp_path, landed=True)
    assert place_idempotent(broker, repo, _order(), reconcile=_perfect(broker)) == "b-1"
    assert (_row(repo).status, _row(repo).broker_order_id) == (PLACED, "b-1")
    assert len(broker.submitted) == 1


def test_inconclusive_refuses_and_keeps_the_anchor(tmp_path: Path) -> None:
    clock = _Clock()
    repo, _, broker = _unknown_row(tmp_path, landed=True, clock=clock)
    anchor = _row(repo).updated_at
    for _ in range(3):
        clock.advance(10)
        with pytest.raises(OrderOutcomeUnknownError, match="inconclusive"):
            place_idempotent(broker, repo, _order(), reconcile=_inconclusive)
    assert _row(repo).updated_at == anchor  # refusals never push the window forward
    assert _row(repo).status == UNKNOWN and len(broker.submitted) == 1


def test_reconciler_that_raises_is_inconclusive(tmp_path: Path) -> None:
    repo, _, broker = _unknown_row(tmp_path, landed=False)

    def _boom(record: OrderRecord) -> ReconcileResult:
        raise ConnectionError("listing failed")

    with pytest.raises(OrderOutcomeUnknownError, match="ConnectionError"):
        place_idempotent(broker, repo, _order(), reconcile=_boom)
    assert _row(repo).status == UNKNOWN


def test_absent_is_not_trusted_for_an_interrupted_send(tmp_path: Path) -> None:
    # Crash mid-send: the row is 'pending' and its updated_at predates the send. ABSENT must
    # not mark it not_placed; the row is re-anchored to 'unknown' at NOW (after the crash).
    clock = _Clock()
    repo, _ = _repo(tmp_path, clock)
    repo._write_pending(_order())
    clock.advance(60)  # restart a minute later
    result = resolve(repo, _row(repo), reconcile=_absent)
    assert result.outcome is ResolveOutcome.UNRESOLVED and "re-anchored" in result.detail
    record = _row(repo)
    assert record.status == UNKNOWN and record.updated_at == NOW + timedelta(seconds=60)
    # A later pass (after the window) may trust ABSENT for the now-unknown row.
    clock.advance(600)
    assert resolve(repo, _row(repo), reconcile=_absent).outcome is ResolveOutcome.NOT_PLACED


def test_interrupted_send_that_landed_is_adopted(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    repo._write_pending(_order())
    broker.submit_order(_order())  # landed; the process died before recording anything
    result = resolve(repo, _row(repo), reconcile=_perfect(broker))
    assert result.outcome is ResolveOutcome.PLACED and result.broker_order_id == "b-1"
    assert _landed(broker) == 1


def test_resolution_is_compare_and_swap(tmp_path: Path) -> None:
    repo, conn, broker = _unknown_row(tmp_path, landed=True)
    stale = _row(repo)
    conn.execute(  # another process resolved the row after we read it (bumping its version)
        "UPDATE orders SET status = ?, updated_at = ?, version = version + 1 "
        "WHERE client_order_id = 'c1'",
        (NOT_PLACED, (NOW + timedelta(seconds=1)).isoformat()),
    )
    result = resolve(repo, stale, reconcile=_perfect(broker))
    assert result.outcome is ResolveOutcome.UNRESOLVED and "concurrently" in result.detail
    assert _row(repo).status == NOT_PLACED  # the newer state was not overwritten


def test_a_broker_id_can_belong_to_only_one_order(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    place_idempotent(broker, repo, _order("c-other"), reconcile=_perfect(broker))  # binds b-1
    repo._write_pending(_order("c1"))
    repo.mark_unknown_after_send("c1")

    def _wrong(record: OrderRecord) -> ReconcileResult:
        return ReconcileResult.found("b-1")  # a reconciler bug: an id already bound

    result = resolve(repo, _row(repo), reconcile=_wrong)
    assert result.outcome is ResolveOutcome.UNRESOLVED and "already bound" in result.detail
    assert _row(repo).broker_order_id is None


def test_not_placed_resolves_without_reconciling(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    repo._write_pending(_order())
    repo.mark_not_placed_after_send("c1")

    def _never(record: OrderRecord) -> ReconcileResult:
        raise AssertionError("a terminal row must not be reconciled")

    assert resolve(repo, _row(repo), reconcile=_never).outcome is ResolveOutcome.NOT_PLACED


def test_reconcile_result_validation() -> None:
    assert ReconcileResult.found("b-1").outcome is ReconcileOutcome.FOUND
    with pytest.raises(ValueError, match="broker_order_id"):
        ReconcileResult(ReconcileOutcome.FOUND)
    with pytest.raises(ValueError, match="broker_order_id"):
        ReconcileResult(ReconcileOutcome.ABSENT, "b-1")


# --- review follow-ups ------------------------------------------------------------ #


def test_resolution_compare_and_swap_uses_the_row_version(tmp_path: Path) -> None:
    # Another writer changed the row without changing its status (e.g. a re-anchor that kept
    # 'unknown'): a resolver holding the stale read must not act on it.
    repo, conn, broker = _unknown_row(tmp_path, landed=True)
    stale = _row(repo)
    conn.execute("UPDATE orders SET version = version + 1 WHERE client_order_id = 'c1'")
    result = resolve(repo, stale, reconcile=_perfect(broker))
    assert result.outcome is ResolveOutcome.UNRESOLVED and "concurrently" in result.detail
    assert _row(repo).broker_order_id is None


def test_every_state_write_bumps_the_version(tmp_path: Path) -> None:
    repo, _, broker = _unknown_row(tmp_path, landed=True)  # pending -> unknown
    assert _row(repo).version == 1
    resolve(repo, _row(repo), reconcile=_perfect(broker))  # unknown -> WORKING (adopt)
    assert (_row(repo).status, _row(repo).version) == (PLACED, 2)


def test_sender_accepts_a_row_already_bound_to_the_same_id(tmp_path: Path) -> None:
    # A resolver adopted the order (same broker id) before the sender recorded it: that is a
    # correctly recorded placement, not an unknown outcome.
    repo, conn = _repo(tmp_path)

    class _AdoptedFirst(FakeBroker):
        def submit_order(self, order: Order) -> str:
            broker_order_id = super().submit_order(order)
            conn.execute(
                "UPDATE orders SET status = ?, broker_order_id = ? WHERE client_order_id = ?",
                (PLACED, broker_order_id, order.client_order_id),
            )
            return broker_order_id

    broker = _AdoptedFirst()
    assert place_idempotent(broker, repo, _order(), reconcile=_perfect(broker)) == "b-1"


def test_unknown_outcome_overrides_a_premature_not_placed(tmp_path: Path) -> None:
    # A resolver (wrongly) marked the row not_placed while the send was in flight, then the
    # send timed out: the order may be live, so the row must go back to 'unknown'.
    repo, conn = _repo(tmp_path)

    class _RacyTimeout(FakeBroker):
        def submit_order(self, order: Order) -> str:
            conn.execute(
                "UPDATE orders SET status = ? WHERE client_order_id = ?",
                (NOT_PLACED, order.client_order_id),
            )
            return super().submit_order(order)

    broker = _RacyTimeout()
    broker.fail_next_submit = True
    broker.record_on_timeout = True
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(broker, repo, _order(), reconcile=_perfect(broker))
    assert _row(repo).status == UNKNOWN


def test_stored_timestamps_parse_robustly(tmp_path: Path) -> None:
    # An operator's SQL fix may store 'Z' or naive timestamps: reads must not break, and a
    # naive value is taken as UTC.
    repo, conn = _repo(tmp_path)
    repo._write_pending(_order())
    conn.execute(
        "UPDATE orders SET created_at = '2026-06-29T15:00:00Z', "
        "updated_at = '2026-06-29 15:00:00' WHERE client_order_id = 'c1'"
    )
    record = _row(repo)
    assert record.created_at == NOW and record.updated_at == NOW


def test_blank_broker_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="broker_order_id"):
        ReconcileResult.found("   ")


def test_repository_reconciliation_queries(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    broker = FakeBroker()
    place_idempotent(broker, repo, _order("c-placed"), reconcile=_perfect(broker))  # b-1
    repo._write_pending(_order("c-pending"))
    broker.fail_next_submit = True
    with pytest.raises(OrderOutcomeUnknownError):
        place_idempotent(broker, repo, _order("c-unknown"), reconcile=_perfect(broker))
    broker.reject_next_submit = True
    with pytest.raises(OrderNotPlacedError):
        place_idempotent(broker, repo, _order("c-rejected"), reconcile=_perfect(broker))
    assert repo.bound_broker_ids() == {"b-1"}
    assert [r.client_order_id for r in repo.awaiting_resolution()] == ["c-pending", "c-unknown"]


def test_reconciliation_coverage_query_and_awaiting_excludes_bound_rows(tmp_path: Path) -> None:
    clock = _Clock()
    repo, conn = _repo(tmp_path, clock)
    broker = FakeBroker()
    place_idempotent(broker, repo, _order("c-early"), reconcile=_perfect(broker))  # b-1 @ NOW
    clock.advance(3600)
    place_idempotent(broker, repo, _order("c-late"), reconcile=_perfect(broker))  # b-2 @ +1h
    window_lo, window_hi = NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)
    assert repo.bound_orders_created_between(window_lo, window_hi) == {"b-1": NOW}
    # A row that somehow carries a broker id is settled, never "awaiting resolution".
    repo._write_pending(_order("c-odd"))
    conn.execute(
        "UPDATE orders SET status = 'unknown', broker_order_id = 'b-9' "
        "WHERE client_order_id = 'c-odd'"
    )
    assert [r.client_order_id for r in repo.awaiting_resolution()] == []


def test_a_raising_reconciler_is_logged_and_inconclusive(tmp_path: Path) -> None:
    import io

    from trader.observability.logging import configure_logging

    repo, _ = _repo(tmp_path)
    repo._write_pending(_order())
    repo.mark_unknown_after_send("c1")
    buf = io.StringIO()
    configure_logging(stream=buf)

    def broken(record: OrderRecord) -> ReconcileResult:
        raise KeyError("wiring bug")

    result = resolve(repo, _row(repo), reconcile=broken)
    assert result.outcome is ResolveOutcome.UNRESOLVED and "KeyError" in result.detail
    assert "reconciler raised" in buf.getvalue() and "Traceback" in buf.getvalue()
