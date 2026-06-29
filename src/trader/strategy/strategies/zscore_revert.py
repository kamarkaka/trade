"""ZScoreRevertStrategy (design §6): mean-reversion on the trailing z-score.

Pure + asof-safe: pulls trailing daily bars ONLY via the injected asof-bound
MarketDataProvider (no-lookahead, Appendix B) and computes the z-score with the shared
``indicators`` library (sample std, ddof=1) so the math matches the backtest + research
harness exactly. The current trigger-instant quote is included as the latest observation.

Signal: BUY ``lot`` when z <= -z_entry (oversold) while flat; SELL ``lot`` when z >= z_entry
(overbought) while flat; when holding and the z reverts inside the exit band (|z| <= z_exit),
emit a closing decision toward flat. HOLD otherwise (insufficient bars or flat series -> z is
None).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.strategy.indicators import closes_from_bars, zscore
from trader.strategy.params import ZScoreRevertParams
from trader.strategy.registry import REGISTRY


@REGISTRY.register("zscore_revert")
class ZScoreRevertStrategy:
    def __init__(self, **params: object) -> None:
        p = ZScoreRevertParams.model_validate(params)  # validate + default (guards direct use)
        self.lookback = p.lookback
        self.z_entry = Decimal(str(p.z_entry))
        self.z_exit = Decimal(str(p.z_exit))
        self.lot = p.lot

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        now = clock.now()
        # Generous calendar window to cover >= lookback TRADING days; the provider is
        # asof-bound, so nothing after `now` is visible (no lookahead).
        start = now - timedelta(days=self.lookback * 4 + 10)
        held_by_symbol = {pos.symbol: pos.quantity for pos in positions}
        decisions: list[Decision] = []
        for symbol, quote in snapshot.quotes.items():
            bars = data.get_bars(symbol, start, now, "daily", asof=now)
            # Include the trigger-instant quote as the latest observation (still asof-bound).
            z = zscore([*closes_from_bars(bars), quote.last], self.lookback)
            if z is None:
                continue  # insufficient history or flat series -> HOLD
            held = held_by_symbol.get(symbol, 0)
            if held == 0 and z <= -self.z_entry:
                decisions.append(
                    Decision(Action.BUY, symbol, self.lot, rationale=f"z={z:.2f} oversold")
                )
            elif held == 0 and z >= self.z_entry:
                decisions.append(
                    Decision(Action.SELL, symbol, self.lot, rationale=f"z={z:.2f} overbought")
                )
            elif held != 0 and abs(z) <= self.z_exit:
                # reverted to the mean -> close the position toward flat
                action = Action.SELL if held > 0 else Action.BUY
                decisions.append(
                    Decision(action, symbol, abs(held), rationale=f"z={z:.2f} exit to flat")
                )
        return decisions
