"""CanaryStrategy: a deterministic, long-only round trip for the guarded live verification
(M5.7 — the first real-money orders, at the smallest size).

The first real order must be small, predictable, and unable to open a short or touch
holdings the bot did not create. For each quoted symbol:

- flat (0 shares) → BUY ``lot``;
- holding exactly ``lot`` → SELL ``lot``;
- anything else (a short, or a different size such as shares bought by hand) → HOLD, so
  the operator resolves it.

Bound to ONE slot per session it buys on one session and sells on the next, so it never
completes a same-session round trip (no day-trade / PDT exposure). Pure: reads only the
snapshot and positions.
"""

from __future__ import annotations

from collections.abc import Sequence

from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.strategy.registry import REGISTRY


@REGISTRY.register("canary")
class CanaryStrategy:
    def __init__(self, lot: int = 1) -> None:
        if lot <= 0:
            raise ValueError(f"lot must be positive, got {lot}")
        self.lot = int(lot)

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        held = {p.symbol: p.quantity for p in positions}
        decisions: list[Decision] = []
        for symbol in sorted(snapshot.quotes):
            quantity = held.get(symbol, 0)
            if quantity == 0:
                decisions.append(Decision(Action.BUY, symbol, self.lot, rationale="canary open"))
            elif quantity == self.lot:
                decisions.append(Decision(Action.SELL, symbol, self.lot, rationale="canary close"))
            # else: an unexpected position (short / other size) -> HOLD for the operator
        return decisions
