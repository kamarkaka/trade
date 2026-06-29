"""Core immutable value types exchanged across the trading system (design §5).

All types are frozen dataclasses. Two invariants are enforced at construction:

* **Money is ``Decimal``** — any monetary field that is not a ``Decimal`` (e.g. a
  ``float``) raises ``TypeError``; ``NaN`` raises ``ValueError``. This keeps the
  whole system off binary floats.
* **Timestamps are timezone-aware UTC** — naive datetimes raise ``ValueError``;
  aware datetimes in other zones are normalized to UTC.

Enum-typed fields accept either an enum member or its string value (coerced).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal

from .enums import (
    Action,
    Distribution,
    DriftDirection,
    OnOvershoot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

# --------------------------------------------------------------------------- #
# Validation helpers (accept ``object`` so runtime/untyped inputs are caught). #
# --------------------------------------------------------------------------- #


def _require_decimal(value: object, name: str, *, nonneg: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__}")
    if value.is_nan():
        raise ValueError(f"{name} must not be NaN")
    if nonneg and value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


def _opt_decimal(value: object, name: str, *, nonneg: bool = False) -> Decimal | None:
    if value is None:
        return None
    return _require_decimal(value, name, nonneg=nonneg)


def _require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _require_int(value: object, name: str, *, positive: bool = False, nonneg: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    if nonneg and value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


# --------------------------------------------------------------------------- #
# Market data                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Quote:
    """A point-in-time quote for one symbol."""

    symbol: str
    ts: datetime  # quote time (tz-aware UTC); used for staleness checks
    last: Decimal
    bid: Decimal
    ask: Decimal
    volume: int
    prev_close: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _require_utc(self.ts, "ts"))
        object.__setattr__(self, "last", _require_decimal(self.last, "last"))
        object.__setattr__(self, "bid", _require_decimal(self.bid, "bid"))
        object.__setattr__(self, "ask", _require_decimal(self.ask, "ask"))
        object.__setattr__(self, "volume", _require_int(self.volume, "volume", nonneg=True))
        object.__setattr__(self, "prev_close", _opt_decimal(self.prev_close, "prev_close"))


@dataclass(frozen=True)
class Bar:
    """An OHLCV candle for one symbol."""

    symbol: str
    ts: datetime  # bar close time (tz-aware UTC)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _require_utc(self.ts, "ts"))
        object.__setattr__(self, "open", _require_decimal(self.open, "open"))
        object.__setattr__(self, "high", _require_decimal(self.high, "high"))
        object.__setattr__(self, "low", _require_decimal(self.low, "low"))
        object.__setattr__(self, "close", _require_decimal(self.close, "close"))
        object.__setattr__(self, "volume", _require_int(self.volume, "volume", nonneg=True))
        if self.high < self.low:
            raise ValueError(f"high ({self.high}) must be >= low ({self.low})")


# --------------------------------------------------------------------------- #
# Account & positions                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Position:
    """A holding in one symbol (quantity is signed; negative = short)."""

    symbol: str
    quantity: int
    avg_price: Decimal
    market_value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _require_int(self.quantity, "quantity"))
        object.__setattr__(
            self, "avg_price", _require_decimal(self.avg_price, "avg_price", nonneg=True)
        )
        object.__setattr__(
            self, "market_value", _require_decimal(self.market_value, "market_value")
        )


@dataclass(frozen=True)
class Account:
    """Account balances."""

    cash: Decimal
    buying_power: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "cash", _require_decimal(self.cash, "cash"))
        object.__setattr__(
            self, "buying_power", _require_decimal(self.buying_power, "buying_power")
        )
        object.__setattr__(self, "equity", _require_decimal(self.equity, "equity"))


# --------------------------------------------------------------------------- #
# Orders & fills                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Order:
    """An order intent. ``client_order_id`` is the idempotency key; ``strategy_id``
    attributes the order to the strategy that produced it."""

    client_order_id: str
    strategy_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None = None
    tif: TimeInForce = TimeInForce.DAY

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "tif", TimeInForce(self.tif))
        object.__setattr__(self, "quantity", _require_int(self.quantity, "quantity", positive=True))
        object.__setattr__(
            self, "limit_price", _opt_decimal(self.limit_price, "limit_price", nonneg=True)
        )
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires a limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET order must not have a limit_price")


@dataclass(frozen=True)
class Fill:
    """A (partial or full) fill / status update for an order."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    quantity: int
    price: Decimal
    fees: Decimal
    ts: datetime
    status: OrderStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", OrderStatus(self.status))
        object.__setattr__(self, "ts", _require_utc(self.ts, "ts"))
        object.__setattr__(self, "quantity", _require_int(self.quantity, "quantity", nonneg=True))
        object.__setattr__(self, "price", _require_decimal(self.price, "price", nonneg=True))
        object.__setattr__(self, "fees", _require_decimal(self.fees, "fees", nonneg=True))


# --------------------------------------------------------------------------- #
# Strategy I/O                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MarketSnapshot:
    """The market view handed to a strategy at a trigger instant."""

    asof: datetime
    quotes: dict[str, Quote]

    def __post_init__(self) -> None:
        object.__setattr__(self, "asof", _require_utc(self.asof, "asof"))
        if not isinstance(self.quotes, dict):
            raise TypeError("quotes must be a dict[str, Quote]")
        for sym, q in self.quotes.items():
            if not isinstance(q, Quote):
                raise TypeError(f"quotes[{sym!r}] must be a Quote, got {type(q).__name__}")


@dataclass(frozen=True)
class Decision:
    """A strategy's decision for one symbol (quantity is an absolute share delta;
    0 for HOLD)."""

    action: Action
    symbol: str
    quantity: int = 0
    limit_price: Decimal | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", Action(self.action))
        object.__setattr__(self, "quantity", _require_int(self.quantity, "quantity", nonneg=True))
        object.__setattr__(
            self, "limit_price", _opt_decimal(self.limit_price, "limit_price", nonneg=True)
        )
        if self.action is Action.HOLD:
            if self.quantity != 0:
                raise ValueError("HOLD decision must have quantity 0")
            if self.limit_price is not None:
                raise ValueError("HOLD decision must not have a limit_price")
        elif self.quantity <= 0:
            raise ValueError(f"{self.action.value} decision must have a positive quantity")


# --------------------------------------------------------------------------- #
# Risk                                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskVerdict:
    """The risk gate's verdict for an order (may clamp instead of reject)."""

    approved: bool
    adjusted_order: Order | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be a bool")
        if self.adjusted_order is not None and not isinstance(self.adjusted_order, Order):
            raise TypeError("adjusted_order must be an Order or None")
        object.__setattr__(self, "reasons", tuple(self.reasons))


# --------------------------------------------------------------------------- #
# Scheduling (config-derived; consumed by the scheduler & orchestrator)        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SlotSpec:
    """One scheduled slot in a strategy's daily schedule (the validated form of a
    config slot)."""

    slot_id: str
    at: time  # local wall-clock time of day
    drift_max_minutes: int
    drift_direction: DriftDirection = DriftDirection.FORWARD
    distribution: Distribution = Distribution.UNIFORM
    on_overshoot: OnOvershoot = OnOvershoot.CLAMP
    catch_up: bool | None = None  # None => inherit the global schedule.catch_up

    def __post_init__(self) -> None:
        if not isinstance(self.at, time):
            raise TypeError(f"at must be a datetime.time, got {type(self.at).__name__}")
        object.__setattr__(
            self,
            "drift_max_minutes",
            _require_int(self.drift_max_minutes, "drift_max_minutes", nonneg=True),
        )
        object.__setattr__(self, "drift_direction", DriftDirection(self.drift_direction))
        object.__setattr__(self, "distribution", Distribution(self.distribution))
        object.__setattr__(self, "on_overshoot", OnOvershoot(self.on_overshoot))
        if self.catch_up is not None and not isinstance(self.catch_up, bool):
            raise TypeError("catch_up must be a bool or None")


@dataclass(frozen=True)
class StrategyBinding:
    """A strategy + its own universe + its own schedule (the runtime form a
    config binding is loaded into; design §5/§6.1)."""

    strategy_id: str
    strategy_name: str
    # dict fields are excluded from __hash__ (dicts are unhashable) so a binding can
    # still be used as a dict key / set member; they remain part of __eq__.
    params: dict[str, object] = field(hash=False)
    universe: tuple[str, ...]
    slots: tuple[SlotSpec, ...]
    enabled: bool = True
    risk_overrides: dict[str, object] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "universe", tuple(self.universe))
        object.__setattr__(self, "slots", tuple(self.slots))
        if self.enabled and not self.universe:
            # an enabled binding must trade something; a disabled one may be empty
            raise ValueError("an enabled binding must have a non-empty universe")
        for s in self.slots:
            if not isinstance(s, SlotSpec):
                raise TypeError(f"slots must contain SlotSpec, got {type(s).__name__}")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")


@dataclass(frozen=True)
class TriggerSlot:
    """A concrete, drift-resolved trigger the scheduler emits and the orchestrator
    dispatches."""

    strategy_id: str
    slot_id: str
    fire_ts: datetime  # scheduled local time + realized drift, normalized to UTC
    drift_seconds: int
    seed: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fire_ts", _require_utc(self.fire_ts, "fire_ts"))
        object.__setattr__(self, "drift_seconds", _require_int(self.drift_seconds, "drift_seconds"))
        if self.seed is not None:
            object.__setattr__(self, "seed", _require_int(self.seed, "seed"))


@dataclass(frozen=True)
class DayState:
    """Per-session risk/accounting state consumed by the risk gate (design §10/§12).

    Populated by the state + reconciliation layers in later milestones; defined
    here so the ``RiskManager`` protocol has a concrete contract to check against.
    """

    trading_date: date
    start_of_day_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    trades_today: int
    loss_today: Decimal
    kill_switch_engaged: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.trading_date, date) or isinstance(self.trading_date, datetime):
            raise TypeError("trading_date must be a datetime.date (not a datetime)")
        object.__setattr__(
            self,
            "start_of_day_equity",
            _require_decimal(self.start_of_day_equity, "start_of_day_equity"),
        )
        object.__setattr__(
            self, "realized_pnl", _require_decimal(self.realized_pnl, "realized_pnl")
        )
        object.__setattr__(
            self, "unrealized_pnl", _require_decimal(self.unrealized_pnl, "unrealized_pnl")
        )
        object.__setattr__(
            self, "trades_today", _require_int(self.trades_today, "trades_today", nonneg=True)
        )
        object.__setattr__(self, "loss_today", _require_decimal(self.loss_today, "loss_today"))
        if not isinstance(self.kill_switch_engaged, bool):
            raise TypeError("kill_switch_engaged must be a bool")


__all__ = [
    "Account",
    "Bar",
    "DayState",
    "Decision",
    "Fill",
    "MarketSnapshot",
    "Order",
    "Position",
    "Quote",
    "RiskVerdict",
    "SlotSpec",
    "StrategyBinding",
    "TriggerSlot",
]
