"""`trader reconcile` against a (fake) live Schwab account (LR9): overrides are all checked
before anything changes — each ``--adopt`` id against the broker's own order — and applied
all or nothing; the exit code says what happened (0 clean, 2 not clean or an override
refused, 4 the broker failed)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes import FakeBroker
from trader.app import cli
from trader.broker import FeesModel
from trader.core import Fill, Order, Position
from trader.core.enums import OrderStatus, OrderType, Side
from trader.execution.idempotency import OrderRecord, OrderRepository, ReconcileResult
from trader.schwab.errors import SchwabServerError
from trader.state.db import connect
from trader.state.migrate import run_migrations

runner = CliRunner()
ACCT = "HASHEDACCT"
NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Reconciler:
    """Stands in for SchwabOrderReconciler: answers WINDOW_OPEN (as for any freshly
    re-anchored row) unless told otherwise, and approves or refuses ``--adopt`` ids."""

    def __init__(self) -> None:
        self.answer = ReconcileResult.inconclusive("inside the consistency window", "window_open")
        self.refuse: str | None = None
        self.verified: list[tuple[str, str]] = []

    def __call__(self, record: OrderRecord) -> ReconcileResult:
        return self.answer

    def verify_binding(self, record: OrderRecord, broker_order_id: str) -> str | None:
        self.verified.append((record.client_order_id, broker_order_id))
        return self.refuse


@dataclass
class _Live:
    config: Path
    broker: FakeBroker
    reconciler: _Reconciler
    repo: OrderRepository
    connected: list[FeesModel | None] = field(default_factory=list)

    def invoke(self, *args: str):  # type: ignore[no-untyped-def]
        return runner.invoke(cli.app, ["reconcile", "--config", str(self.config), *args])

    def unknown(self, cid: str, symbol: str = "AAPL", qty: int = 1) -> None:
        self.repo._write_pending(Order(cid, "s1", symbol, Side.BUY, qty, OrderType.MARKET))
        self.repo.mark_unknown_after_send(cid)

    def row(self, cid: str) -> OrderRecord:
        record = self.repo.get(cid)
        assert record is not None
        return record


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Live:
    config = tmp_path / "live.yaml"
    config.write_text(
        f"""
mode: live
strategies:
  - id: s1
    name: threshold
    universe: [AAPL]
    slots:
      - {{id: open, time: "09:45"}}
execution:
  fees_model: {{commission: "0.65", regulatory_bps: 0}}
observability:
  data_cache: "{tmp_path}"
  db_path: "{tmp_path / "state.sqlite"}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    state = _Live(config, FakeBroker(), _Reconciler(), OrderRepository(conn))

    def fake_live_account(cfg, http, repo, *, clock, fees, command):  # type: ignore[no-untyped-def]
        state.connected.append(fees)
        return cli._LiveAccount(state.broker, state.reconciler, ACCT)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_live_account", fake_live_account)
    return state


def _filled(broker: FakeBroker, broker_order_id: str, symbol: str = "AAPL", qty: int = 1) -> None:
    broker._fills[broker_order_id] = Fill(
        "", broker_order_id, symbol, qty, Decimal("100"), Decimal("0"), NOW, OrderStatus.FILLED
    )
    broker.set_position(Position(symbol, qty, Decimal("100"), Decimal(100 * qty)))


def test_a_clean_account_exits_0(live: _Live) -> None:
    result = live.invoke()
    assert result.exit_code == 0 and "result: CLEAN" in result.output


def test_an_account_that_is_not_clean_exits_2(live: _Live) -> None:
    live.broker.set_position(Position("TSLA", 5, Decimal("200"), Decimal("1000")))  # by hand
    result = live.invoke()
    assert result.exit_code == 2 and "TSLA: broker 5" in result.output


def test_a_broker_failure_exits_4_without_echoing_its_text(live: _Live) -> None:
    def down() -> list[Position]:
        raise SchwabServerError(f"accounts/{ACCT} returned 503")

    live.broker.get_positions = down  # type: ignore[method-assign]
    result = live.invoke()
    assert result.exit_code == 4 and "SchwabServerError" in result.output
    assert ACCT not in result.output


def test_completed_fills_carry_the_configured_fees(live: _Live) -> None:
    live.invoke()
    assert live.connected == [FeesModel(commission=Decimal("0.65"), regulatory_bps=0.0)]


def test_an_adopted_id_is_checked_against_the_broker_order_first(live: _Live) -> None:
    live.unknown("c1")
    live.reconciler.refuse = "it differs from the order in: symbol"
    result = live.invoke("--adopt", "c1=SCH-7")
    assert result.exit_code == 2 and "differs from the order in: symbol" in result.output
    assert live.reconciler.verified == [("c1", "SCH-7")]
    assert live.row("c1").broker_order_id is None  # nothing bound


def test_a_verified_adoption_is_completed_in_the_same_run(live: _Live) -> None:
    live.unknown("c1")
    _filled(live.broker, "SCH-7")
    result = live.invoke("--adopt", "c1=SCH-7")
    assert "override: c1 adopted as SCH-7" in result.output
    assert result.exit_code == 0 and "result: CLEAN" in result.output
    assert (live.row("c1").status, live.row("c1").broker_order_id) == ("FILLED", "SCH-7")


def test_overrides_apply_together_after_every_check(live: _Live) -> None:
    live.unknown("c-gone")
    live.unknown("c-found")
    _filled(live.broker, "SCH-7")
    result = live.invoke("--mark-not-placed", "c-gone", "--adopt", "c-found=SCH-7")
    assert "override: c-gone marked not placed" in result.output
    assert "override: c-found adopted as SCH-7" in result.output
    assert live.row("c-gone").status == "not_placed" and live.row("c-found").status == "FILLED"
    assert result.exit_code == 0


def test_one_refused_adoption_blocks_every_other_override(live: _Live) -> None:
    live.unknown("c-gone")
    live.unknown("c-found")
    live.reconciler.refuse = "it differs from the order in: quantity"
    result = live.invoke("--mark-not-placed", "c-gone", "--adopt", "c-found=SCH-7")
    assert result.exit_code == 2 and "nothing was changed" in result.output
    assert live.row("c-gone").status == "unknown"


def test_unbind_returns_a_wrongly_bound_order_to_resolution(live: _Live) -> None:
    live.repo._write_pending(Order("c1", "s1", "AAPL", Side.BUY, 1, OrderType.MARKET))
    live.repo.record_placed("c1", "SCH-WRONG")
    result = live.invoke("--unbind", "c1")
    assert "override: c1 unbound from SCH-WRONG" in result.output
    assert (live.row("c1").status, live.row("c1").broker_order_id) == ("unknown", None)
    assert result.exit_code == 2 and "window_open" in result.output  # resolved afresh later


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--adopt", "no-equals"], "CID=BROKER_ORDER_ID"),
        (["--adopt", "c1=SCH-7", "--mark-not-placed", "c1"], "more than one override"),
        (["--adopt", "c1=SCH-7", "--adopt", "c2=SCH-7"], "adopted by more than one order"),
        (["--mark-not-placed", "nope"], "no order nope"),
        (["--unbind", "c1"], "not a placed, uncompleted order"),  # c1 has no broker id
        (["--adopt", "c1=SCH-1"], "SCH-1 is already bound to order c-bound"),
    ],
)
def test_bad_overrides_exit_2_before_the_broker_is_contacted(
    live: _Live, args: list[str], message: str
) -> None:
    live.unknown("c1")
    live.unknown("c2")
    live.repo._write_pending(Order("c-bound", "s1", "AAPL", Side.BUY, 1, OrderType.MARKET))
    live.repo.record_placed("c-bound", "SCH-1")
    result = live.invoke(*args)
    assert result.exit_code == 2 and message in result.output
    assert live.connected == []  # refused before connecting
    assert live.row("c1").status == "unknown"
