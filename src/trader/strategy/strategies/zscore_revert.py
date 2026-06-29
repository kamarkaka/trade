"""ZScoreRevertStrategy (design §6): mean-reversion on the trailing z-score.

Pure + asof-safe: pulls trailing daily bars ONLY via the injected asof-bound
MarketDataProvider, so it inherits no-lookahead (Appendix B). Computes the mean/std
of the last ``lookback`` closes; BUY ``lot`` when z <= -z_entry (oversold), SELL when
z >= z_entry. HOLD (no decision) on insufficient bars or zero std (no div-by-zero).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.strategy.registry import REGISTRY


@REGISTRY.register("zscore_revert")
class ZScoreRevertStrategy:
    def __init__(self, lookback: int = 20, z_entry: float = 2.0, lot: int = 10) -> None:
        self.lookback = lookback
        self.z_entry = Decimal(str(z_entry))
        self.lot = lot

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        # Generous calendar window to cover >= lookback *trading* days; the provider
        # is asof-bound so nothing after snapshot.asof is visible.
        start = snapshot.asof - timedelta(days=self.lookback * 4 + 10)
        decisions: list[Decision] = []
        for symbol, quote in snapshot.quotes.items():
            bars = data.get_bars(symbol, start, snapshot.asof, "daily", asof=snapshot.asof)
            closes = [b.close for b in bars][-self.lookback :]
            if len(closes) < self.lookback:
                continue  # insufficient history -> hold
            mean = sum(closes, Decimal(0)) / Decimal(len(closes))
            variance = sum(((c - mean) ** 2 for c in closes), Decimal(0)) / Decimal(len(closes))
            std = variance.sqrt()
            if std == 0:
                continue  # flat series -> hold (no div-by-zero)
            z = (quote.last - mean) / std
            if z <= -self.z_entry:
                decisions.append(Decision(Action.BUY, symbol, self.lot, rationale=f"z={z:.2f}"))
            elif z >= self.z_entry:
                decisions.append(Decision(Action.SELL, symbol, self.lot, rationale=f"z={z:.2f}"))
        return decisions
