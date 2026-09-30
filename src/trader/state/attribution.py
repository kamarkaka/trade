"""Per-strategy position attribution (design §10 #16).

Each fill updates a sub-position tagged by ``strategy_id`` (average-cost, signed), so
two strategies trading the same symbol keep strictly separate books. What the broker holds
beyond the strategies' sums is *unattributed* (``unattributed``). The special ``'unknown'``
strategy holds the operator-acknowledged baseline of unattributed holdings (e.g. the owner's
own positions in the same account); only ``accept_unattributed`` — the operator's explicit
act — writes it (see ``execution.reconcile``).

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


@dataclass(frozen=True)
class BaselineChange:
    """A symbol whose acknowledged unattributed quantity was set from ``old`` to ``new``."""

    symbol: str
    old: int
    new: int


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

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def apply(self, fill: Fill, strategy_id: str, side: Side) -> None:
        # SELECT-then-upsert is safe under the single global cycle lock (§7.5), which
        # serializes the only writer; it would be a read-modify-write race without it.
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

    def unattributed(self, broker_positions: Sequence[Position]) -> dict[str, int]:
        """Per symbol, the broker's quantity minus the real (non-'unknown') attributed sum —
        only where they differ. Read-only."""
        real = {
            sym: int(qty)
            for sym, qty in self._conn.execute(
                "SELECT symbol, SUM(quantity) FROM attributed_position "
                "WHERE strategy_id != ? GROUP BY symbol",
                (UNKNOWN,),
            ).fetchall()
        }
        broker = {p.symbol: p.quantity for p in broker_positions}
        return {
            symbol: broker.get(symbol, 0) - real.get(symbol, 0)
            for symbol in sorted(set(real) | set(broker))
            if broker.get(symbol, 0) != real.get(symbol, 0)
        }

    def accept_unattributed(self, broker_positions: Sequence[Position]) -> list[BaselineChange]:
        """Make the current unattributed quantities the acknowledged baseline (the 'unknown'
        rows), atomically; returns every symbol whose baseline changed."""
        avg_price = {p.symbol: p.avg_price for p in broker_positions}
        self._conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            before = {p.symbol: p.quantity for p in self.get_attributed(UNKNOWN)}
            after = self.unattributed(broker_positions)
            changes = [
                BaselineChange(symbol, before.get(symbol, 0), after.get(symbol, 0))
                for symbol in sorted(set(before) | set(after))
                if before.get(symbol, 0) != after.get(symbol, 0)
            ]
            for change in changes:  # a new quantity of 0 deletes the row
                self._upsert(
                    UNKNOWN, change.symbol, change.new, avg_price.get(change.symbol, Decimal(0))
                )
            self._conn.execute("COMMIT")
            committed = True
            return changes
        finally:
            if not committed and self._conn.in_transaction:
                self._conn.execute("ROLLBACK")

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
