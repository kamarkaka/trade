"""Event-driven backtest engine — single strategy (design Appendix A, §9).

The shared core loop: advance the VirtualClock to each trigger, build a
point-in-time MarketSnapshot, run ``strategy.decide`` → size → submit to the
SimBroker, and feed fills to the Portfolio. The SAME loop structure runs live
(M3 daemon) — only the injected Clock / MarketDataProvider / Broker differ.

No-lookahead is structural via **deferred fills**: decisions made at trigger *i*
(seeing only data ``<= i``) are submitted and filled at trigger *i+1*, so an order
never fills on the same bar the decision observed. (Multi-strategy interleave,
the trading calendar, schedule jitter, and richer sizing arrive in M3; ``seed`` is
accepted now for that forward-compatibility but is unused while there is no jitter.)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from trader.core import Fill, MarketSnapshot, Order, Quote
from trader.core.enums import Action, OrderType, Side
from trader.core.protocols import Broker, Clock, MarketDataProvider, Strategy

from .portfolio import Portfolio


@dataclass(frozen=True)
class BacktestResult:
    """Everything a run produced: the realized fills and the equity curve."""

    fills: list[Fill]
    equity_curve: list[tuple[datetime, Decimal]]


class BacktestEngine:
    """Runs one strategy over historical data with a virtual clock."""

    def __init__(
        self,
        *,
        clock: Clock,
        data: MarketDataProvider,
        broker: Broker,
        portfolio: Portfolio,
    ) -> None:
        self._clock = clock
        self._data = data
        self._broker = broker
        self._portfolio = portfolio

    def run(
        self,
        strategy: Strategy,
        *,
        universe: Sequence[str],
        slots: Sequence[time],
        start: date,
        end: date,
        strategy_id: str = "bt",
        seed: int = 0,
    ) -> BacktestResult:
        fills: list[Fill] = []
        pending: list[tuple[Order, Side]] = []  # orders decided last trigger, fill now
        order_seq = 0

        for fire_ts in _triggers(start, end, slots):
            self._advance(fire_ts)
            quotes = self._snapshot_quotes(universe, fire_ts)

            # 1) Fill orders decided at the previous trigger against THIS bar.
            # NOTE: with the default uncapped SimBroker these fill fully in one shot.
            # A partially-filled remainder (only possible once ADV caps are configured)
            # is NOT yet carried across triggers — that lifecycle is wired in M3.
            for order, side in pending:
                fill = self._broker.get_order(self._broker.submit_order(order))
                if fill.quantity > 0:
                    self._portfolio.apply_fill(fill, side)
                    fills.append(fill)
            pending = []

            # 2) Mark to the current quotes and snapshot the equity curve.
            if quotes:
                self._portfolio.mark_to_market(quotes)
            self._portfolio.snapshot(fire_ts)

            # 3) Decide for the NEXT trigger using only data available now.
            snapshot = MarketSnapshot(asof=fire_ts, quotes=quotes)
            decisions = strategy.decide(
                snapshot,
                self._broker.get_positions(),
                self._broker.get_account(),
                self._data,
                self._clock,
            )
            for decision in decisions:
                if decision.action is Action.HOLD or decision.quantity <= 0:
                    continue  # quantity<=0 is belt-and-suspenders (Decision rejects it)
                order_seq += 1
                side = Side.BUY if decision.action is Action.BUY else Side.SELL
                pending.append(
                    (
                        Order(
                            client_order_id=f"{strategy_id}:{order_seq}",
                            strategy_id=strategy_id,
                            symbol=decision.symbol,
                            side=side,
                            quantity=decision.quantity,
                            order_type=OrderType.MARKET,
                        ),
                        side,
                    )
                )

        # The final trigger's decisions remain in `pending` and are intentionally
        # discarded — there is no next bar to fill them against (no-lookahead).
        return BacktestResult(fills=fills, equity_curve=self._portfolio.equity_series())

    def _advance(self, fire_ts: datetime) -> None:
        # Backtest drives a VirtualClock forward to each trigger. Live (M3) injects a
        # RealClock with no advance_to — wall-clock time flows on its own — so this
        # intentionally no-ops there rather than forcing the time.
        advance_to = getattr(self._clock, "advance_to", None)
        if callable(advance_to):
            advance_to(fire_ts)

    def _snapshot_quotes(self, universe: Sequence[str], asof: datetime) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for symbol in universe:
            try:
                quotes[symbol] = self._data.get_quote(symbol, asof)
            except (LookupError, ValueError):
                continue  # no data at/before asof yet -> symbol absent this trigger
        return quotes


def _triggers(start: date, end: date, slots: Sequence[time]) -> list[datetime]:
    """Chronological trigger instants for each date in ``[start, end]`` x ``slots``."""
    ordered_slots = sorted(slots)
    triggers: list[datetime] = []
    day = start
    while day <= end:
        triggers.extend(datetime.combine(day, slot, tzinfo=UTC) for slot in ordered_slots)
        day += timedelta(days=1)
    return triggers
