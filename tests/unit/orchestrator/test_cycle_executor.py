"""The orchestrator's durable execution path (LR5): with an executor injected, an approved
order is written ahead, placed at most once, polled to terminal and completed atomically;
a broker refusal is audited and the cycle moves on; an unknown outcome fails the cycle."""

import itertools
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeBroker, FakeClock, FakeMarketDataProvider
from trader.config.models import ExecutionConfig
from trader.core import Account, Decision, MarketSnapshot, Order, Position, Quote
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.execution.executor import DurableOrderExecutor, in_memory_reconciler
from trader.execution.idempotency import OrderRepository
from trader.execution.poller import PollPolicy
from trader.orchestrator.cycle import ListAuditSink, Orchestrator
from trader.orchestrator.lock import NullLock
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _quote(symbol: str) -> Quote:
    p = Decimal("100")
    return Quote(symbol, NOW, p, p, p, 1000, prev_close=p)


class _Decide:
    def __init__(self, decisions: Sequence[Decision]) -> None:
        self._decisions = decisions

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return self._decisions


class _RejectsMSFT(FakeBroker):
    """Definitely refuses MSFT orders (e.g. a validation 4xx); fills the rest."""

    def submit_order(self, order: Order) -> str:
        if order.symbol == "MSFT":
            self.reject_next_submit = True
        return super().submit_order(order)


def _setup(tmp_path: Path, broker: FakeBroker):  # type: ignore[no-untyped-def]
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    attribution = AttributionLedger(conn)
    repo = OrderRepository(conn)
    executor = DurableOrderExecutor(
        broker=broker,
        repo=repo,
        attribution=attribution,
        reconcile=in_memory_reconciler(broker.find_by_client_id),
        poll_policy=PollPolicy(timeout_seconds=0),
        sleep=lambda _s: None,
    )
    ids = (f"o{i}" for i in itertools.count())
    audit = ListAuditSink()
    orch = Orchestrator(
        broker=broker,
        data=FakeMarketDataProvider(quotes={s: [_quote(s)] for s in ("AAPL", "MSFT", "SPY")}),
        clock=FakeClock(NOW),
        cycle_lock=NullLock(),
        attribution=attribution,
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids)),
        audit=audit,
        executor=executor,
    )
    return orch, conn, attribution, audit


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple[object, ...]]:
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def test_approved_order_is_written_placed_and_completed(tmp_path: Path) -> None:
    orch, conn, attribution, audit = _setup(tmp_path, FakeBroker())
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "s1", NOW)
    assert [f.quantity for f in result.fills] == [10] and result.errors == []
    assert _rows(conn, "SELECT client_order_id, status, broker_order_id FROM orders") == [
        ("o0", "FILLED", "b-1")
    ]
    assert _rows(conn, "SELECT client_order_id, quantity FROM fills") == [("o0", 10)]
    assert {p.symbol: p.quantity for p in attribution.get_attributed("s1")} == {"AAPL": 10}
    assert [e.kind for e in audit.events] == ["order_pending", "fill"]


def test_a_broker_refusal_is_audited_and_the_cycle_moves_on(tmp_path: Path) -> None:
    orch, conn, _, audit = _setup(tmp_path, _RejectsMSFT())
    decisions = [Decision(Action.BUY, "MSFT", 1), Decision(Action.BUY, "SPY", 1)]
    result = orch.run_cycle(_Decide(decisions), ["MSFT", "SPY"], "s1", NOW)
    assert [o.symbol for o in result.not_placed] == ["MSFT"]
    assert [f.symbol for f in result.fills] == ["SPY"] and result.errors == []
    kinds = [(e.kind, e.payload.get("symbol")) for e in audit.events]
    assert ("order_not_placed", "MSFT") in kinds and ("fill", "SPY") in kinds
    statuses = dict(_rows(conn, "SELECT client_order_id, status FROM orders"))
    assert statuses == {"o0": "not_placed", "o1": "FILLED"}


def test_an_unknown_outcome_fails_the_cycle_before_any_further_order(tmp_path: Path) -> None:
    broker = FakeBroker()
    broker.fail_next_submit = True  # the first send's outcome is unknown
    orch, conn, attribution, _ = _setup(tmp_path, broker)
    decisions = [Decision(Action.BUY, "AAPL", 1), Decision(Action.BUY, "SPY", 1)]
    result = orch.run_cycle(_Decide(decisions), ["AAPL", "SPY"], "s1", NOW)
    assert result.errors and "outcome unknown" in result.errors[0]
    assert len(broker.submitted) == 1  # SPY was never sent while AAPL's fate is uncertain
    assert dict(_rows(conn, "SELECT client_order_id, status FROM orders")) == {"o0": "unknown"}
    assert attribution.get_attributed("s1") == []
