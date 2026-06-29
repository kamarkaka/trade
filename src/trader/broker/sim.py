"""SimBroker — the deterministic simulated broker (design §9.3).

Implements the core ``Broker`` protocol for backtest/paper so the SAME strategy and
risk code runs against simulated execution.

* **MARKET** orders fill at the next quote: ``ask + slippage`` (BUY) /
  ``bid - slippage`` (SELL). The engine advances the clock between decision and
  fill, so the quote read here is post-decision (never fill at the signal instant).
* **LIMIT** orders fill only when the current bar's ``[low, high]`` range crosses
  the limit (BUY: ``low <= limit``; SELL: ``high >= limit``), at the limit price.
* Fills are capped at ``max_participation`` * bar/quote volume (ADV cap); the
  unfilled remainder is carried as a WORKING order and re-evaluated on later bars
  (``process_working_orders``). DAY orders expire at session close
  (``expire_day_orders``).

A ``FeesModel`` (Schwab $0 commission + sell-side regulatory bps) is applied so
backtest P&L tracks live economics. Money keeps full ``Decimal`` precision (no penny
rounding) — intentional for a deterministic backtest; live reconciliation (M4) rounds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trader.core import Account, Bar, Fill, Order, Position, Quote
from trader.core.enums import OrderStatus, OrderType, Side, TimeInForce
from trader.core.protocols import Clock, MarketDataProvider

# How far back to look for the "current" daily bar when evaluating a limit order.
_BAR_LOOKBACK = timedelta(days=7)


@dataclass(frozen=True)
class SlippageModel:
    """Adverse price movement applied to a market fill. ``kind``: 'bps' | 'fixed'."""

    kind: str = "bps"
    value: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.kind not in {"bps", "fixed", "vol"}:
            raise ValueError(f"unknown slippage kind {self.kind!r}")

    def amount(self, reference_price: Decimal) -> Decimal:
        if self.kind == "bps":
            return reference_price * self.value / Decimal(10000)
        if self.kind == "fixed":
            return self.value
        raise NotImplementedError(f"slippage kind {self.kind!r} is not supported yet")

    @classmethod
    def from_config(cls, cfg: object) -> SlippageModel:
        return cls(kind=cfg.type, value=Decimal(str(cfg.value)))  # type: ignore[attr-defined]


@dataclass(frozen=True)
class FeesModel:
    """Per-fill fees: a flat commission plus regulatory bps on notional."""

    commission: Decimal = Decimal("0")
    regulatory_bps: float = 0.0

    def fee(self, notional: Decimal, side: Side) -> Decimal:
        # Regulatory (SEC/TAF) fees are sell-side only; commission applies both sides.
        regulatory = (
            notional * Decimal(str(self.regulatory_bps)) / Decimal(10000)
            if side is Side.SELL
            else Decimal("0")
        )
        return self.commission + regulatory

    @classmethod
    def from_config(cls, cfg: object) -> FeesModel:
        return cls(
            commission=cfg.commission,  # type: ignore[attr-defined]
            regulatory_bps=cfg.regulatory_bps,  # type: ignore[attr-defined]
        )


@dataclass
class _Lot:
    """Mutable per-symbol holding: signed quantity, cost basis, last mark."""

    quantity: int
    avg_price: Decimal
    last_price: Decimal


@dataclass
class _Working:
    """An order being worked: cumulative fills so far + the original intent."""

    order: Order
    broker_order_id: str
    filled_qty: int = 0
    cost: Decimal = field(default_factory=lambda: Decimal("0"))  # sum(qty*price)
    fees: Decimal = field(default_factory=lambda: Decimal("0"))
    last_fill_ts: datetime | None = None  # bar/quote ts of the last fill (per-bar budget)


class SimBroker:
    """A deterministic, in-memory simulated broker (market + limit, partials)."""

    def __init__(
        self,
        data: MarketDataProvider,
        clock: Clock,
        *,
        starting_cash: Decimal,
        fees: FeesModel | None = None,
        slippage: SlippageModel | None = None,
        max_participation: Decimal | None = None,
    ) -> None:
        self._data = data
        self._clock = clock
        self._cash = Decimal(starting_cash)
        self._fees = fees or FeesModel()
        self._slippage = slippage or SlippageModel()
        self._max_participation = max_participation
        self._lots: dict[str, _Lot] = {}
        self._fills: dict[str, Fill] = {}  # broker_order_id -> latest cumulative Fill
        self._working: dict[str, _Working] = {}  # broker_order_id -> open order
        self._by_client: dict[str, str] = {}  # client_order_id -> broker_order_id
        self._seq = 0

    # --- Broker protocol -------------------------------------------------- #

    def submit_order(self, order: Order) -> str:
        existing = self._by_client.get(order.client_order_id)
        if existing is not None:  # idempotent: never double-submit
            return existing

        broker_order_id = f"SIM-{self._seq + 1}"
        working = _Working(order=order, broker_order_id=broker_order_id)
        self._try_fill(working)  # may raise (negative price) before _seq is committed
        self._seq += 1
        status = self._status_of(working)
        self._fills[broker_order_id] = self._snapshot(working, status)
        self._by_client[order.client_order_id] = broker_order_id
        if status is not OrderStatus.FILLED:
            self._working[broker_order_id] = working  # carry the remainder
        return broker_order_id

    def get_order(self, broker_order_id: str) -> Fill:
        try:
            return self._fills[broker_order_id]
        except KeyError as exc:
            raise KeyError(f"unknown broker_order_id {broker_order_id!r}") from exc

    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id not in self._fills:
            raise KeyError(f"unknown broker_order_id {broker_order_id!r}")
        working = self._working.pop(broker_order_id, None)
        if working is not None:  # a fully-filled order is a no-op to cancel
            self._fills[broker_order_id] = self._snapshot(working, OrderStatus.CANCELED)

    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol=symbol,
                quantity=lot.quantity,
                avg_price=lot.avg_price,
                market_value=Decimal(lot.quantity) * self._mark_price(symbol, lot.last_price),
            )
            for symbol, lot in self._lots.items()
            if lot.quantity != 0
        ]

    def get_account(self) -> Account:
        market_value = sum(
            (
                Decimal(lot.quantity) * self._mark_price(sym, lot.last_price)
                for sym, lot in self._lots.items()
            ),
            Decimal("0"),
        )
        return Account(cash=self._cash, buying_power=self._cash, equity=self._cash + market_value)

    # --- engine-driven lifecycle ----------------------------------------- #

    def process_working_orders(self) -> None:
        """Re-evaluate open orders against the current bar/quote (call each bar)."""
        for broker_order_id, working in list(self._working.items()):
            self._try_fill(working)
            status = self._status_of(working)
            self._fills[broker_order_id] = self._snapshot(working, status)
            if status is OrderStatus.FILLED:
                del self._working[broker_order_id]

    def expire_day_orders(self) -> None:
        """Expire still-open DAY orders at session close."""
        for broker_order_id, working in list(self._working.items()):
            if working.order.tif is TimeInForce.DAY:
                self._fills[broker_order_id] = self._snapshot(working, OrderStatus.EXPIRED)
                del self._working[broker_order_id]

    # --- fill engine ------------------------------------------------------ #

    def _try_fill(self, working: _Working) -> None:
        order = working.order
        remaining = order.quantity - working.filled_qty
        if remaining <= 0:
            return

        if order.order_type is OrderType.MARKET:
            quote = self._data.get_quote(order.symbol, self._clock.now())
            data_ts = quote.ts
            price = self._fill_price(order.side, quote)
            available_volume = quote.volume
        else:  # LIMIT
            bar = self._current_bar(order.symbol)
            if bar is None or order.limit_price is None:
                return  # no bar to evaluate against -> stays working
            if not self._limit_crosses(order.side, order.limit_price, bar):
                return
            data_ts = bar.ts
            price = order.limit_price  # limit guarantees price; no extra slippage
            available_volume = bar.volume

        # One participation budget per bar: don't re-consume the same bar across calls.
        if working.last_fill_ts == data_ts:
            return

        if price < 0:
            # Guard before mutating any state (atomicity): a fill price can't be negative.
            raise ValueError(f"computed negative fill price {price} for {order.symbol!r}")

        fill_qty = remaining
        if self._max_participation is not None:
            cap = int(self._max_participation * Decimal(available_volume))
            if cap == 0 and available_volume > 0:
                cap = 1  # floor so a low-volume order isn't starved forever
            fill_qty = min(remaining, cap)
        if fill_qty <= 0:
            return

        notional = Decimal(fill_qty) * price
        fee = self._fees.fee(notional, order.side)
        self._apply_cash(order.side, notional, fee)
        self._apply_position(order.symbol, order.side, fill_qty, price)
        working.filled_qty += fill_qty
        working.cost += notional
        working.fees += fee
        working.last_fill_ts = data_ts

    def _status_of(self, working: _Working) -> OrderStatus:
        if working.filled_qty >= working.order.quantity:
            return OrderStatus.FILLED
        if working.filled_qty > 0:
            return OrderStatus.PARTIAL_FILL
        return OrderStatus.WORKING

    def _snapshot(self, working: _Working, status: OrderStatus) -> Fill:
        qty = working.filled_qty
        price = (working.cost / qty) if qty > 0 else Decimal("0")  # VWAP across partials
        return Fill(
            client_order_id=working.order.client_order_id,
            broker_order_id=working.broker_order_id,
            symbol=working.order.symbol,
            quantity=qty,
            price=price,
            fees=working.fees,
            ts=self._clock.now(),
            status=status,
        )

    def _current_bar(self, symbol: str) -> Bar | None:
        now = self._clock.now()
        bars = self._data.get_bars(symbol, now - _BAR_LOOKBACK, now, "daily", now)
        return bars[-1] if bars else None

    @staticmethod
    def _limit_crosses(side: Side, limit: Decimal, bar: Bar) -> bool:
        if side is Side.BUY:
            return bar.low <= limit
        return bar.high >= limit

    def _mark_price(self, symbol: str, fallback: Decimal) -> Decimal:
        """Current mark from the data feed (mark-to-market); last fill if unavailable."""
        try:
            return self._data.get_quote(symbol, self._clock.now()).last
        except (LookupError, ValueError):
            return fallback

    def _fill_price(self, side: Side, quote: Quote) -> Decimal:
        if side is Side.BUY:
            return quote.ask + self._slippage.amount(quote.ask)
        return quote.bid - self._slippage.amount(quote.bid)

    def _apply_cash(self, side: Side, notional: Decimal, fees: Decimal) -> None:
        if side is Side.BUY:
            self._cash -= notional + fees
        else:
            self._cash += notional - fees

    def _apply_position(self, symbol: str, side: Side, quantity: int, price: Decimal) -> None:
        lot = self._lots.get(symbol, _Lot(0, Decimal("0"), price))
        signed = quantity if side is Side.BUY else -quantity
        old_qty = lot.quantity
        new_qty = old_qty + signed

        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            # opening or increasing the same direction -> weighted-average cost basis
            new_avg = (
                (abs(old_qty) * lot.avg_price + quantity * price) / abs(new_qty)
                if new_qty != 0
                else Decimal("0")
            )
        elif abs(signed) <= abs(old_qty):
            new_avg = lot.avg_price  # reducing the position: basis unchanged
        else:
            new_avg = price  # flipped through zero: basis resets to the new side

        lot.quantity = new_qty
        lot.avg_price = new_avg if new_qty != 0 else Decimal("0")
        lot.last_price = price
        self._lots[symbol] = lot
