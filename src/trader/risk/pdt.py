"""Pattern-day-trader (PDT) rule (design §10).

Counts day-trades (a same-session round trip in one symbol) over a rolling business-day
window and blocks the order that would be one too many while account equity is below the
threshold. **Configurable, not hardcoded** — the thresholds live in ``RiskConfig`` and are
**[VERIFY]** against current FINRA Rule 4210 (a 2026 amendment may change the regime). The
``enforce_pdt`` flag disables it entirely (e.g. cash accounts, where T+1 settlement / good-
faith rules apply instead).

``Fill`` carries no side, so the rule operates on explicit ``TradeEvent`` (symbol + side +
timestamp) — the caller builds these from the persisted orders/fills. PDT only bites on a
real margin account, so the gate wiring (supplying the trade history + the calendar-derived
window start) is assembled on the live order path at go-live (M5.6/M5.7); paper never
day-trades a real account.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import Order
from trader.core.enums import Side
from trader.risk.rules import RuleResult


@dataclass(frozen=True)
class TradeEvent:
    """One executed side for a symbol (built from orders/fills by the caller)."""

    symbol: str
    side: Side
    ts: datetime


class PDTRule:
    def __init__(self, config: RiskConfig) -> None:
        self._cfg = config

    def count_day_trades(self, events: Sequence[TradeEvent], *, window_start: date) -> int:
        """Number of day-trades (a symbol+session with BOTH a buy and a sell) on or after
        ``window_start``."""
        sessions: dict[tuple[str, date], set[Side]] = {}
        for e in events:
            d = e.ts.date()
            if d < window_start:
                continue
            sessions.setdefault((e.symbol, d), set()).add(e.side)
        return sum(1 for sides in sessions.values() if {Side.BUY, Side.SELL} <= sides)

    def _completes_day_trade(
        self, order: Order, events: Sequence[TradeEvent], *, asof: datetime
    ) -> bool:
        """True if ``order`` closes a position opened (opposite side) in the SAME session —
        i.e. it would complete a new round-trip day-trade."""
        today = asof.date()
        opposite = Side.SELL if order.side is Side.BUY else Side.BUY
        return any(
            e.symbol == order.symbol and e.ts.date() == today and e.side is opposite for e in events
        )

    def check(
        self,
        order: Order,
        *,
        events: Sequence[TradeEvent],
        equity: Decimal,
        asof: datetime,
        window_start: date,
    ) -> RuleResult:
        """Block the order if it would be the (max+1)th day-trade while equity is under the
        threshold and enforcement is on."""
        if not self._cfg.enforce_pdt:
            return RuleResult(ok=True)
        if equity >= self._cfg.pdt_equity_threshold_usd:
            return RuleResult(ok=True)  # PDT only applies under the equity threshold
        count = self.count_day_trades(events, window_start=window_start)
        if count >= self._cfg.pdt_max_day_trades and self._completes_day_trade(
            order, events, asof=asof
        ):
            return RuleResult(
                ok=False,
                reason=(
                    f"PDT: {count} day-trades in window; a 4th is blocked while equity "
                    f"< {self._cfg.pdt_equity_threshold_usd}"
                ),
            )
        return RuleResult(ok=True)


__all__ = ["PDTRule", "TradeEvent"]
