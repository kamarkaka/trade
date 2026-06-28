"""Tests for the core value types: Decimal-money + tz-aware-UTC invariants,
immutability, enum coercion, and per-type domain rules."""

import dataclasses
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from trader.core import (
    Account,
    Bar,
    DayState,
    Decision,
    Fill,
    MarketSnapshot,
    Order,
    Position,
    Quote,
    RiskVerdict,
    SlotSpec,
    StrategyBinding,
    TriggerSlot,
)
from trader.core.enums import (
    Action,
    DriftDirection,
    OnOvershoot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

T = datetime(2026, 6, 28, 14, 30, tzinfo=UTC)
D = Decimal


def make_quote(**kw: object) -> Quote:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "ts": T,
        "last": D("100.50"),
        "bid": D("100.49"),
        "ask": D("100.51"),
        "volume": 1000,
    }
    base.update(kw)
    return Quote(**base)  # type: ignore[arg-type]


def make_order(**kw: object) -> Order:
    base: dict[str, object] = {
        "client_order_id": "c-1",
        "strategy_id": "momentum",
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 10,
        "order_type": OrderType.MARKET,
    }
    base.update(kw)
    return Order(**base)  # type: ignore[arg-type]


# --- Decimal-money invariant ------------------------------------------------ #


def test_money_is_decimal() -> None:
    with pytest.raises(TypeError):
        make_quote(last=100.5)  # float, not Decimal


def test_money_rejects_nan() -> None:
    with pytest.raises(ValueError):
        make_quote(last=Decimal("NaN"))


def test_negative_fee_rejected() -> None:
    with pytest.raises(ValueError):
        Fill(
            client_order_id="c-1",
            broker_order_id="b-1",
            symbol="AAPL",
            quantity=1,
            price=D("100"),
            fees=D("-0.01"),
            ts=T,
            status=OrderStatus.FILLED,
        )


# --- tz-aware-UTC invariant ------------------------------------------------- #


def test_timestamps_tz_aware() -> None:
    with pytest.raises(ValueError):
        make_quote(ts=datetime(2026, 6, 28, 14, 30))  # naive


def test_timestamps_normalized_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    q = make_quote(ts=datetime(2026, 6, 28, 10, 30, tzinfo=eastern))  # 14:30 UTC
    assert q.ts.utcoffset() == timedelta(0)
    assert q.ts == T


# --- immutability ----------------------------------------------------------- #


def test_quote_is_frozen() -> None:
    q = make_quote()
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.last = D("1")  # type: ignore[misc]


def test_order_is_frozen() -> None:
    o = make_order()
    with pytest.raises(dataclasses.FrozenInstanceError):
        o.quantity = 5  # type: ignore[misc]


# --- Order invariants ------------------------------------------------------- #


def test_order_requires_client_and_strategy_id() -> None:
    with pytest.raises(TypeError):
        Order(symbol="AAPL", side=Side.BUY, quantity=1, order_type=OrderType.MARKET)  # type: ignore[call-arg]
    o = make_order()
    assert o.client_order_id == "c-1"
    assert o.strategy_id == "momentum"


def test_order_quantity_must_be_positive_int() -> None:
    with pytest.raises(ValueError):
        make_order(quantity=0)
    with pytest.raises(TypeError):
        make_order(quantity=1.5)
    with pytest.raises(TypeError):
        make_order(quantity=True)  # bool is not a valid quantity


def test_limit_order_requires_price() -> None:
    with pytest.raises(ValueError):
        make_order(order_type=OrderType.LIMIT)  # no limit_price
    with pytest.raises(ValueError):
        make_order(order_type=OrderType.MARKET, limit_price=D("100"))
    o = make_order(order_type=OrderType.LIMIT, limit_price=D("100"))
    assert o.order_type is OrderType.LIMIT


def test_order_coerces_enum_strings() -> None:
    o = make_order(side="SELL", order_type="LIMIT", limit_price=D("100"), tif="GTC")
    assert o.side is Side.SELL
    assert o.order_type is OrderType.LIMIT
    assert o.tif is TimeInForce.GTC
    with pytest.raises(ValueError):
        make_order(side="HOLD")  # not a valid Side


# --- Bar / Decision rules --------------------------------------------------- #


def test_bar_high_must_be_ge_low() -> None:
    with pytest.raises(ValueError):
        Bar(symbol="AAPL", ts=T, open=D("10"), high=D("9"), low=D("11"), close=D("10"), volume=1)


def test_decision_hold_must_be_zero_quantity() -> None:
    with pytest.raises(ValueError):
        Decision(action=Action.HOLD, symbol="AAPL", quantity=5)
    hold = Decision(action=Action.HOLD, symbol="AAPL")
    assert hold.quantity == 0


def test_decision_trade_requires_positive_quantity() -> None:
    with pytest.raises(ValueError):
        Decision(action=Action.BUY, symbol="AAPL", quantity=0)
    d = Decision(action="BUY", symbol="AAPL", quantity=3)
    assert d.action is Action.BUY


# --- containers & scheduling ------------------------------------------------ #


def test_market_snapshot_rejects_non_quote() -> None:
    with pytest.raises(TypeError):
        MarketSnapshot(asof=T, quotes={"AAPL": "nope"})  # type: ignore[dict-item]
    snap = MarketSnapshot(asof=T, quotes={"AAPL": make_quote()})
    assert snap.quotes["AAPL"].symbol == "AAPL"


def test_risk_verdict_reasons_coerced_to_tuple() -> None:
    v = RiskVerdict(approved=False, reasons=["over notional", "stale quote"])
    assert v.reasons == ("over notional", "stale quote")
    assert isinstance(v.reasons, tuple)


def test_strategy_binding_requires_universe() -> None:
    with pytest.raises(ValueError):
        StrategyBinding(
            strategy_id="m", strategy_name="threshold", params={}, universe=(), slots=()
        )
    b = StrategyBinding(
        strategy_id="m",
        strategy_name="threshold",
        params={"band": 0.02},
        universe=["AAPL", "MSFT"],
        slots=[SlotSpec(slot_id="am", at=time(9, 45), drift_max_minutes=30)],
    )
    assert b.universe == ("AAPL", "MSFT")
    assert b.slots[0].drift_direction is DriftDirection.FORWARD


def test_slot_spec_coerces_config_strings() -> None:
    s = SlotSpec(
        slot_id="am",
        at=time(9, 45),
        drift_max_minutes=20,
        drift_direction="symmetric",
        distribution="uniform",
        on_overshoot="skip",
    )
    assert s.drift_direction is DriftDirection.SYMMETRIC
    assert s.on_overshoot is OnOvershoot.SKIP


def test_trigger_slot_normalizes_utc() -> None:
    ts = TriggerSlot(
        strategy_id="m",
        slot_id="am",
        fire_ts=datetime(2026, 6, 28, 10, 30, tzinfo=timezone(timedelta(hours=-4))),
        drift_seconds=443,
        seed=12345,
    )
    assert ts.fire_ts == T


def test_constructs_account_and_position() -> None:
    acct = Account(cash=D("1000"), buying_power=D("2000"), equity=D("1500"))
    pos = Position(symbol="AAPL", quantity=-5, avg_price=D("100"), market_value=D("-500"))
    assert acct.equity == D("1500")
    assert pos.quantity == -5  # short positions allowed


def make_day_state(**kw: object) -> DayState:
    base: dict[str, object] = {
        "trading_date": date(2026, 6, 28),
        "start_of_day_equity": D("10000"),
        "realized_pnl": D("0"),
        "unrealized_pnl": D("0"),
        "trades_today": 0,
        "loss_today": D("0"),
    }
    base.update(kw)
    return DayState(**base)  # type: ignore[arg-type]


def test_day_state_constructs_and_defaults_kill_switch_off() -> None:
    ds = make_day_state(trades_today=3, loss_today=D("-120.50"))
    assert ds.kill_switch_engaged is False
    assert ds.trades_today == 3


def test_day_state_trading_date_must_be_plain_date() -> None:
    with pytest.raises(TypeError):
        make_day_state(trading_date=datetime(2026, 6, 28, tzinfo=UTC))  # datetime, not date
    with pytest.raises(TypeError):
        make_day_state(trading_date="2026-06-28")


def test_day_state_validates_money_and_counts() -> None:
    with pytest.raises(TypeError):
        make_day_state(start_of_day_equity=10000.0)  # float
    with pytest.raises(ValueError):
        make_day_state(trades_today=-1)
    with pytest.raises(TypeError):
        make_day_state(kill_switch_engaged="yes")
