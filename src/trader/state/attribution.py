"""Per-strategy position attribution (design §10 #16).

Each fill updates a sub-position tagged by ``strategy_id`` (average-cost, signed), so
two strategies trading the same symbol keep strictly separate books. ``reconcile_total``
compares the attributed sums to the broker's true positions and parks any unattributed
delta under the special ``'unknown'`` strategy (so the books always tie out).

``Fill`` carries no side, so ``apply`` takes it explicitly (the orchestrator has the
originating order).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trader.core import Fill, Position
from trader.core.enums import Side

UNKNOWN = "unknown"


@dataclass(frozen=True)
class AttributedPosition:
    strategy_id: str
    symbol: str
    quantity: int
    avg_price: Decimal


def _apply_avg(old_qty: int, old_avg: Decimal, signed: int, price: Decimal) -> tuple[int, Decimal]:
    new_qty = old_qty + signed
    if new_qty == 0:
        return 0, Decimal("0")
    if old_qty == 0 or (old_qty > 0) == (signed > 0):  # open / increase same side
        return new_qty, (abs(old_qty) * old_avg + abs(signed) * price) / abs(new_qty)
    if abs(signed) <= abs(old_qty):  # reduce: basis unchanged
        return new_qty, old_avg
    return new_qty, price  # flipped through zero


class AttributionLedger:
    """Durable per-strategy attributed sub-positions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def apply(self, fill: Fill, strategy_id: str, side: Side) -> None:
        if fill.quantity == 0:
            return
        signed = fill.quantity if side is Side.BUY else -fill.quantity
        row = self._conn.execute(
            "SELECT quantity, avg_price FROM attributed_position "
            "WHERE strategy_id = ? AND symbol = ?",
            (strategy_id, fill.symbol),
        ).fetchone()
        old_qty, old_avg = (int(row[0]), Decimal(row[1])) if row is not None else (0, Decimal("0"))
        new_qty, new_avg = _apply_avg(old_qty, old_avg, signed, fill.price)
        self._upsert(strategy_id, fill.symbol, new_qty, new_avg)

    def get_attributed(self, strategy_id: str) -> list[AttributedPosition]:
        rows = self._conn.execute(
            "SELECT symbol, quantity, avg_price FROM attributed_position "
            "WHERE strategy_id = ? ORDER BY symbol",
            (strategy_id,),
        ).fetchall()
        return [
            AttributedPosition(strategy_id, sym, int(qty), Decimal(avg)) for sym, qty, avg in rows
        ]

    def reconcile_total(self, broker_positions: Sequence[Position]) -> list[AttributedPosition]:
        """Park (broker - attributed) under 'unknown' for every symbol that doesn't tie out."""
        attributed = {
            sym: int(qty)
            for sym, qty in self._conn.execute(
                "SELECT symbol, SUM(quantity) FROM attributed_position GROUP BY symbol"
            ).fetchall()
        }
        broker = {p.symbol: p for p in broker_positions}
        parked: list[AttributedPosition] = []
        for symbol in sorted(set(attributed) | set(broker)):
            broker_pos = broker.get(symbol)
            broker_qty = broker_pos.quantity if broker_pos is not None else 0
            delta = broker_qty - attributed.get(symbol, 0)
            if delta == 0:
                continue
            avg = broker_pos.avg_price if broker_pos is not None else Decimal("0")
            self._upsert(UNKNOWN, symbol, delta, avg)
            parked.append(AttributedPosition(UNKNOWN, symbol, delta, avg))
        return parked

    def _upsert(self, strategy_id: str, symbol: str, quantity: int, avg_price: Decimal) -> None:
        if quantity == 0:
            self._conn.execute(
                "DELETE FROM attributed_position WHERE strategy_id = ? AND symbol = ?",
                (strategy_id, symbol),
            )
            return
        self._conn.execute(
            "INSERT INTO attributed_position (strategy_id, symbol, quantity, avg_price) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(strategy_id, symbol) "
            "DO UPDATE SET quantity = excluded.quantity, avg_price = excluded.avg_price",
            (strategy_id, symbol, quantity, str(avg_price)),
        )
