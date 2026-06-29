"""Portfolio & P&L tracking for the backtest (design §9).

Consumes fills to maintain cash, per-symbol average-cost positions, realized P&L,
and (via mark-to-market) unrealized P&L, and appends equity snapshots that the
report (M2.10) and metrics (M6.5) consume. Everything is ``Decimal``.

Invariant: ``equity() == starting_cash + realized_pnl() + unrealized_pnl()`` (realized
is net of all fees; fees also flow through cash). ``Fill`` carries no side, so
``apply_fill`` takes the side explicitly — the engine has the originating order.

Per-strategy attribution is layered on in M3; this is the single-book view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.core import Fill, Quote
from trader.core.enums import Side


@dataclass
class _Lot:
    quantity: int  # signed (negative = short)
    avg_cost: Decimal


class Portfolio:
    """Single-book cash/positions/P&L tracker driven by fills."""

    def __init__(self, starting_cash: Decimal) -> None:
        self._starting_cash = Decimal(starting_cash)
        self._cash = Decimal(starting_cash)
        self._lots: dict[str, _Lot] = {}
        self._realized = Decimal("0")
        self._fees = Decimal("0")
        self._marks: dict[str, Decimal] = {}
        self._equity_series: list[tuple[datetime, Decimal]] = []

    def apply_fill(self, fill: Fill, side: Side) -> None:
        """Apply one execution (incremental qty/price/fees) for ``side``."""
        qty = fill.quantity
        if qty == 0:  # a WORKING/EXPIRED/CANCELED snapshot with nothing executed
            return
        price = fill.price
        signed = qty if side is Side.BUY else -qty

        # cash + fees
        if side is Side.BUY:
            self._cash -= Decimal(qty) * price + fill.fees
        else:
            self._cash += Decimal(qty) * price - fill.fees
        self._fees += fill.fees
        self._realized -= fill.fees  # realized P&L is net of fees

        lot = self._lots.get(fill.symbol, _Lot(0, Decimal("0")))
        old_qty = lot.quantity

        # realized P&L on the portion that reduces an existing opposite position
        if old_qty != 0 and (old_qty > 0) != (signed > 0):
            closed = min(abs(signed), abs(old_qty))
            if old_qty > 0:  # closing a long
                self._realized += (price - lot.avg_cost) * closed
            else:  # closing a short
                self._realized += (lot.avg_cost - price) * closed

        # update position (average-cost)
        new_qty = old_qty + signed
        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            lot.avg_cost = (
                (abs(old_qty) * lot.avg_cost + qty * price) / abs(new_qty)
                if new_qty != 0
                else Decimal("0")
            )
        elif abs(signed) > abs(old_qty):
            lot.avg_cost = price  # flipped through zero
        # (pure reduction leaves avg_cost unchanged)
        lot.quantity = new_qty
        # load-bearing: this is what zeros the basis on an exact full close (the
        # pure-reduction branch above doesn't fire when abs(signed) == abs(old_qty)).
        lot.avg_cost = lot.avg_cost if new_qty != 0 else Decimal("0")
        self._lots[fill.symbol] = lot
        self._marks[fill.symbol] = price  # last trade marks the symbol

    def mark_to_market(self, quotes: dict[str, Quote]) -> None:
        for symbol, quote in quotes.items():
            self._marks[symbol] = quote.last

    def _mark(self, symbol: str, lot: _Lot) -> Decimal:
        # apply_fill always seeds _marks at the fill price, so a held symbol is
        # normally present; avg_cost is the conservative default before any mark.
        return self._marks.get(symbol, lot.avg_cost)

    def realized_pnl(self) -> Decimal:
        return self._realized

    def unrealized_pnl(self) -> Decimal:
        return sum(
            (
                (self._mark(symbol, lot) - lot.avg_cost) * lot.quantity
                for symbol, lot in self._lots.items()
                if lot.quantity != 0
            ),
            Decimal("0"),
        )

    def total_fees(self) -> Decimal:
        return self._fees

    def cash(self) -> Decimal:
        return self._cash

    def equity(self) -> Decimal:
        """Cash + mark-to-market value of positions. Call ``mark_to_market`` with the
        current quotes first for a live mark (otherwise positions mark at last fill)."""
        market_value = sum(
            (Decimal(lot.quantity) * self._mark(symbol, lot) for symbol, lot in self._lots.items()),
            Decimal("0"),
        )
        return self._cash + market_value

    def positions(self) -> dict[str, tuple[int, Decimal]]:
        """symbol -> (signed quantity, average cost) for open positions."""
        return {s: (lot.quantity, lot.avg_cost) for s, lot in self._lots.items() if lot.quantity}

    def snapshot(self, ts: datetime) -> None:
        """Append an equity-curve point at ``ts``."""
        self._equity_series.append((ts, self.equity()))

    def equity_series(self) -> list[tuple[datetime, Decimal]]:
        return list(self._equity_series)
