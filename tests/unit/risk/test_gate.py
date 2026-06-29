"""Tests for the RiskManager gate: dual-scope limit merge, conflict policy, typed
verdicts, and the single-chokepoint behaviour (M4.3)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from trader.clock.virtual import VirtualClock
from trader.config.models import RiskConfig
from trader.core import Account, DayState, Decision, Order, Quote, RiskVerdict
from trader.core.enums import Action, ConflictPolicy, OrderType, Side
from trader.core.protocols import RiskManager as RiskManagerProto
from trader.risk.gate import ResolvedDecision, RiskManager

NOW = datetime(2024, 7, 8, 15, 0, tzinfo=UTC)
ACCOUNT = Account(cash=Decimal("100000"), buying_power=Decimal("100000"), equity=Decimal("100000"))
DAY = DayState(
    trading_date=date(2024, 7, 8),
    start_of_day_equity=Decimal("100000"),
    realized_pnl=Decimal("0"),
    unrealized_pnl=Decimal("0"),
    trades_today=0,
    loss_today=Decimal("0"),
)
QUOTE = Quote("AAPL", NOW, Decimal("100"), Decimal("99.5"), Decimal("100.5"), 1000)


def _order(side: Side = Side.BUY, qty: int = 10, strategy_id: str = "s1", cid: str = "c1") -> Order:
    return Order(cid, strategy_id, "AAPL", side, qty, OrderType.MARKET)


def _gate(
    *,
    account: RiskConfig | None = None,
    overrides: dict[str, dict[str, object]] | None = None,
    default_policy: ConflictPolicy | None = None,
    priority_order: tuple[str, ...] = (),
) -> RiskManager:
    return RiskManager(
        account_config=account or RiskConfig(),
        clock=VirtualClock(NOW),
        overrides_by_strategy=overrides,
        default_policy=default_policy,
        priority_order=priority_order,
    )


def _check(gate: RiskManager, order: Order, **kw: object) -> RiskVerdict:
    positions = kw.get("positions", ())
    return gate.check(order, positions, ACCOUNT, QUOTE, kw.get("day_state", DAY))  # type: ignore[arg-type]


# --- chokepoint basics ------------------------------------------------------ #


def test_gate_implements_protocol() -> None:
    assert isinstance(_gate(), RiskManagerProto)


def test_clean_order_approved() -> None:
    verdict = _check(_gate(), _order(qty=10))  # 1000 notional, well within defaults
    assert verdict.approved is True
    assert verdict.adjusted_order is None
    assert verdict.reasons == ()


def test_reject_returns_typed_verdict_with_reasons() -> None:
    gate = _gate(account=RiskConfig(denylist=("AAPL",)))
    verdict = _check(gate, _order())
    assert isinstance(verdict, RiskVerdict)
    assert verdict.approved is False
    assert verdict.reasons  # non-empty, explains why
    assert any("denylist" in r for r in verdict.reasons)


# --- dual scope ------------------------------------------------------------- #


def test_per_strategy_overrides_merge() -> None:
    # Account allows $5000/order; the strategy's stricter $500 override wins and clamps.
    gate = _gate(overrides={"s1": {"max_order_notional_usd": Decimal("500")}})
    verdict = _check(gate, _order(qty=10))  # 10 * 100 = 1000 > 500 -> clamp to 5
    assert verdict.approved is True
    assert verdict.adjusted_order is not None
    assert verdict.adjusted_order.quantity == 5


def test_account_wide_is_hard_cap() -> None:
    # Strategy override loosens every per-strategy cap, but account-wide gross still bites.
    gate = _gate(
        overrides={
            "s1": {
                "max_order_notional_usd": Decimal("10000000"),
                "max_gross_exposure_usd": Decimal("100000000"),
                "max_position_size_pct": 100.0,
            }
        }
    )
    verdict = _check(gate, _order(qty=300))  # 300 * 100 = 30000 > account 25000 cap
    assert verdict.approved is False
    assert any("account-wide" in r and "max_gross_exposure" in r for r in verdict.reasons)


def test_per_strategy_position_cap_rejects_when_account_allows() -> None:
    # Per-strategy stricter position cap rejects even though account-wide is fine.
    gate = _gate(overrides={"s1": {"max_position_size_pct": 1.0}})  # 1% => 10 share cap
    verdict = _check(gate, _order(qty=50))  # 50 > 10
    assert verdict.approved is False
    assert any("per-strategy" in r and "max_position_size" in r for r in verdict.reasons)


# --- clamp + idempotency ---------------------------------------------------- #


def test_notional_clamp_emits_adjusted_order_and_note() -> None:
    gate = _gate(account=RiskConfig(max_order_notional_usd=Decimal("500")))
    verdict = _check(gate, _order(qty=10))
    assert verdict.approved is True
    assert verdict.adjusted_order is not None and verdict.adjusted_order.quantity == 5
    assert verdict.adjusted_order.client_order_id == "c1"  # identity preserved through clamp
    assert verdict.reasons  # carries the clamp note


def test_duplicate_guard_after_approval() -> None:
    gate = _gate()
    first = _check(gate, _order(cid="dup1"))
    assert first.approved is True
    second = _check(gate, _order(cid="dup1"))  # same client_order_id resubmitted
    assert second.approved is False
    assert any("duplicate" in r for r in second.reasons)


# --- conflict policy -------------------------------------------------------- #


def _d(action: Action, symbol: str = "AAPL", qty: int = 10) -> Decision:
    return Decision(action, symbol, qty)


def test_conflict_net_nets_same_ticker() -> None:
    gate = _gate(default_policy=ConflictPolicy.NET)
    resolved = gate.resolve_conflicts(
        [("a", _d(Action.BUY, qty=10)), ("b", _d(Action.SELL, qty=4))]
    )
    assert len(resolved) == 1
    r = resolved[0]
    assert (r.symbol, r.action, r.quantity) == ("AAPL", Action.BUY, 6)
    assert r.contributors == (("a", 10), ("b", -4))


def test_conflict_net_offsetting_drops_to_flat() -> None:
    gate = _gate()
    resolved = gate.resolve_conflicts([("a", _d(Action.BUY, qty=5)), ("b", _d(Action.SELL, qty=5))])
    assert resolved == []  # nets to zero -> no order, never cross our own spread


def test_conflict_independent_keeps_both() -> None:
    gate = _gate()
    decisions = [("a", _d(Action.BUY, qty=10)), ("b", _d(Action.SELL, qty=4))]
    resolved = gate.resolve_conflicts(decisions, ConflictPolicy.INDEPENDENT)
    assert len(resolved) == 2
    assert {(r.action, r.quantity) for r in resolved} == {(Action.BUY, 10), (Action.SELL, 4)}


def test_conflict_priority_highest_wins_others_dropped() -> None:
    gate = _gate(priority_order=("b", "a"))  # 'b' outranks 'a'
    decisions = [("a", _d(Action.BUY, qty=10)), ("b", _d(Action.SELL, qty=4))]
    resolved = gate.resolve_conflicts(decisions, ConflictPolicy.PRIORITY)
    assert len(resolved) == 1
    assert (resolved[0].action, resolved[0].quantity) == (Action.SELL, 4)


def test_conflict_policy_defaults_from_account_config() -> None:
    gate = RiskManager(
        account_config=RiskConfig(conflict_policy=ConflictPolicy.INDEPENDENT),
        clock=VirtualClock(NOW),
    )
    resolved = gate.resolve_conflicts(
        [("a", _d(Action.BUY, qty=10)), ("b", _d(Action.SELL, qty=4))]
    )
    assert len(resolved) == 2  # independent default honoured


def test_resolve_drops_holds_and_zero() -> None:
    gate = _gate()
    resolved = gate.resolve_conflicts(
        [("a", Decision(Action.HOLD, "AAPL", 0)), ("b", _d(Action.BUY, qty=7))]
    )
    assert len(resolved) == 1
    assert isinstance(resolved[0], ResolvedDecision)
    assert resolved[0].quantity == 7
