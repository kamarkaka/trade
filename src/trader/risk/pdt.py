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
from zoneinfo import ZoneInfo

from trader.config.models import RiskConfig
from trader.core import Order
from trader.core.enums import Side
from trader.risk.rules import RuleResult

# The trading "session" is the EXCHANGE-tz calendar date, NOT the UTC date: a same-session
# round trip late in the US afternoon crosses into the next UTC day, so UTC bucketing would
# split it and under-count day-trades (a fail-open on a regulatory gate).
_DEFAULT_SESSION_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TradeEvent:
    """One executed side for a symbol (built from orders/fills by the caller)."""

    symbol: str
    side: Side
    ts: datetime


class PDTRule:
    def __init__(self, config: RiskConfig, *, session_tz: ZoneInfo = _DEFAULT_SESSION_TZ) -> None:
        self._cfg = config
        self._tz = session_tz

    def _session_date(self, ts: datetime) -> date:
        return ts.astimezone(self._tz).date()

    def count_day_trades(self, events: Sequence[TradeEvent], *, window_start: date) -> int:
        """Number of day-trades on or after ``window_start`` (an exchange-tz session date).

        A day-trade is a buy+sell round trip in one symbol in one session; multiple round
        trips in the same symbol/session each count (approximated as ``min(buys, sells)`` —
        conservative without lot matching), so the rule never under-counts toward the limit."""
        sessions: dict[tuple[str, date], list[int]] = {}  # (symbol, session) -> [buys, sells]
        for e in events:
            d = self._session_date(e.ts)
            if d < window_start:
                continue
            cell = sessions.setdefault((e.symbol, d), [0, 0])
            cell[0 if e.side is Side.BUY else 1] += 1
        return sum(min(buys, sells) for buys, sells in sessions.values())

    def _completes_day_trade(
        self, order: Order, events: Sequence[TradeEvent], *, asof: datetime
    ) -> bool:
        """True if ``order`` closes a position opened (opposite side) in the SAME session —
        i.e. it would complete a new round-trip day-trade."""
        today = self._session_date(asof)
        opposite = Side.SELL if order.side is Side.BUY else Side.BUY
        return any(
            e.symbol == order.symbol and self._session_date(e.ts) == today and e.side is opposite
            for e in events
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
