"""ThresholdStrategy (design §6): buy a dip / sell a pop relative to prev close.

Pure: reads only the injected snapshot/positions/account/data/clock; Decimal math.
For each quote with a known ``prev_close``: BUY ``lot`` when ``last < prev_close*(1-band)``,
SELL when ``last > prev_close*(1+band)``, else hold. Quotes without a prev_close are
skipped. HOLD is expressed by emitting no decision (the engine treats an omitted and
an explicit HOLD identically).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.strategy.registry import REGISTRY


@REGISTRY.register("threshold")
class ThresholdStrategy:
    def __init__(self, band: float = 0.02, lot: int = 10) -> None:
        self.band = Decimal(str(band))
        self.lot = lot

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        decisions: list[Decision] = []
        for symbol, quote in snapshot.quotes.items():
            if quote.prev_close is None:
                continue
            lower = quote.prev_close * (Decimal(1) - self.band)
            upper = quote.prev_close * (Decimal(1) + self.band)
            if quote.last < lower:
                decisions.append(Decision(Action.BUY, symbol, self.lot, rationale="dip"))
            elif quote.last > upper:
                decisions.append(Decision(Action.SELL, symbol, self.lot, rationale="pop"))
        return decisions
