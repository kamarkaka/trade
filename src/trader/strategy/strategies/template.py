"""Copy-paste template strategy (design §6; see docs/strategy_guide.md).

ExampleTemplateStrategy is a minimal, fully-conformant strategy you can copy to start a new
one. It already passes the M6.1 conformance suite. The inline comments map each boundary rule
(§4.1) to the code; fill in the TODO with your own signal.

Signal here (a placeholder): compare the latest price to the simple moving average of recent
closes — BUY below the average, SELL above it. Swap this for your own logic.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.strategy.indicators import closes_from_bars, sma
from trader.strategy.registry import REGISTRY


@REGISTRY.register("template")
class ExampleTemplateStrategy:
    """A conformant example. Replace the body of ``decide`` with your own signal."""

    def __init__(self, lookback: int = 20, lot: int = 10, **_params: object) -> None:
        # Keep params typed + simple; they come from the binding's `params:` (config §11).
        self.lookback = int(lookback)
        self.lot = int(lot)

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],  # your CURRENT holdings (read-only; never mutate)
        account: Account,  # cash/buying_power/equity (read-only)
        data: MarketDataProvider,  # asof-bound history; the ONLY way to read bars
        clock: Clock,  # the ONLY time source — never the wall clock
    ) -> Sequence[Decision]:
        # Boundary rule 1: read ONLY the injected snapshot/positions/account/data/clock.
        now = clock.now()
        # Generous calendar window to cover >= lookback TRADING days; the provider is
        # asof-bound (Appendix B), so nothing after `now` is ever visible (no lookahead).
        start = now - timedelta(days=self.lookback * 4 + 10)
        decisions: list[Decision] = []
        for symbol, quote in snapshot.quotes.items():
            bars = data.get_bars(symbol, start=start, end=now, freq="1d", asof=now)
            average = sma(closes_from_bars(bars), self.lookback)
            if average is None:
                continue  # insufficient history -> HOLD (emit nothing)
            # TODO: replace this placeholder signal with your own.
            if quote.last < average:
                decisions.append(Decision(Action.BUY, symbol, self.lot, rationale="below SMA"))
            elif quote.last > average:
                decisions.append(Decision(Action.SELL, symbol, self.lot, rationale="above SMA"))
            # else: HOLD (no decision). Sizing + risk happen later in the orchestrator —
            # return the desired ABSOLUTE share delta, not dollar amounts.
        return decisions
