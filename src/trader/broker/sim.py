"""SimBroker — the deterministic simulated broker (design §9.3).

Implements the core ``Broker`` protocol for backtest/paper so the SAME strategy and
risk code runs against simulated execution. Market orders fill at the **next**
available quote (the engine advances the clock between decision and fill, so the
quote read here is post-decision) at ``ask + slippage`` (BUY) / ``bid - slippage``
(SELL), with a ``FeesModel`` (Schwab $0 commission + regulatory bps) applied so
backtest P&L tracks live economics. Cash and positions are tracked in memory.

This module owns market-order fills; limit + partial fills are added in M2.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trader.core import Account, Fill, Order, Position, Quote
from trader.core.enums import OrderStatus, OrderType, Side
from trader.core.protocols import Clock, MarketDataProvider

# Lazy config import only for the from_config helpers (avoid a hard config dep here).


@dataclass(frozen=True)
class SlippageModel:
    """Adverse price movement applied to a fill. ``kind``: 'bps' | 'fixed'."""

    kind: str = "bps"
    value: Decimal = Decimal("0")

    def amount(self, reference_price: Decimal) -> Decimal:
        if self.kind == "bps":
            return reference_price * self.value / Decimal(10000)
        if self.kind == "fixed":
            return self.value
        raise NotImplementedError(f"slippage kind {self.kind!r} is not supported yet")

    @classmethod
    def from_config(cls, cfg: object) -> SlippageModel:
        # cfg is a SlippageModelConfig (type, value); kept loose to avoid the import.
        return cls(kind=cfg.type, value=Decimal(str(cfg.value)))  # type: ignore[attr-defined]


@dataclass(frozen=True)
class FeesModel:
    """Per-fill fees: a flat commission plus regulatory bps on notional."""

    commission: Decimal = Decimal("0")
    regulatory_bps: float = 0.0

    def fee(self, notional: Decimal) -> Decimal:
        regulatory = notional * Decimal(str(self.regulatory_bps)) / Decimal(10000)
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


class SimBroker:
    """A deterministic, in-memory simulated broker for market orders."""

    def __init__(
        self,
        data: MarketDataProvider,
        clock: Clock,
        *,
        starting_cash: Decimal,
        fees: FeesModel | None = None,
        slippage: SlippageModel | None = None,
    ) -> None:
        self._data = data
        self._clock = clock
        self._cash = Decimal(starting_cash)
        self._fees = fees or FeesModel()
        self._slippage = slippage or SlippageModel()
        self._lots: dict[str, _Lot] = {}
        self._orders: dict[str, Fill] = {}  # broker_order_id -> Fill
        self._by_client: dict[str, str] = {}  # client_order_id -> broker_order_id
        self._seq = 0

    # --- Broker protocol -------------------------------------------------- #

    def submit_order(self, order: Order) -> str:
        # Idempotent: a re-submitted client_order_id never double-fills.
        existing = self._by_client.get(order.client_order_id)
        if existing is not None:
            return existing
        if order.order_type is not OrderType.MARKET:
            raise NotImplementedError("SimBroker supports only MARKET orders in M2.5 (limit: M2.6)")

        quote = self._data.get_quote(order.symbol, self._clock.now())
        price = self._fill_price(order.side, quote)
        notional = Decimal(order.quantity) * price
        fees = self._fees.fee(notional)
        self._apply_cash(order.side, notional, fees)
        self._apply_position(order.symbol, order.side, order.quantity, price)

        self._seq += 1
        broker_order_id = f"SIM-{self._seq}"
        fill = Fill(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            symbol=order.symbol,
            quantity=order.quantity,
            price=price,
            fees=fees,
            ts=self._clock.now(),
            status=OrderStatus.FILLED,
        )
        self._orders[broker_order_id] = fill
        self._by_client[order.client_order_id] = broker_order_id
        return broker_order_id

    def get_order(self, broker_order_id: str) -> Fill:
        try:
            return self._orders[broker_order_id]
        except KeyError as exc:
            raise KeyError(f"unknown broker_order_id {broker_order_id!r}") from exc

    def cancel_order(self, broker_order_id: str) -> None:
        # Market orders fill synchronously, so there is nothing working to cancel;
        # this only validates the id exists.
        if broker_order_id not in self._orders:
            raise KeyError(f"unknown broker_order_id {broker_order_id!r}")

    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol=symbol,
                quantity=lot.quantity,
                avg_price=lot.avg_price,
                market_value=Decimal(lot.quantity) * lot.last_price,
            )
            for symbol, lot in self._lots.items()
            if lot.quantity != 0
        ]

    def get_account(self) -> Account:
        market_value = sum(
            (Decimal(lot.quantity) * lot.last_price for lot in self._lots.values()),
            Decimal("0"),
        )
        equity = self._cash + market_value
        return Account(cash=self._cash, buying_power=self._cash, equity=equity)

    # --- internals -------------------------------------------------------- #

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
