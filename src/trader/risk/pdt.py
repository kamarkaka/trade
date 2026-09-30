"""Pattern-day-trader (PDT) rule (design §10).

Counts day-trades (a same-session round trip in one symbol) over a rolling window of exchange
sessions and, while account equity is below the threshold, blocks the order that would be one
too many — and, once at the limit, any new entry: a position opened then could not be closed
in the same session without a violation, so its intraday exits (stop-losses) would be
blocked. **Configurable, not hardcoded** — the thresholds live in ``RiskConfig`` and are
**[VERIFY]** against current FINRA Rule 4210 (a 2026 amendment may change the regime). The
``enforce_pdt`` flag disables it entirely (e.g. cash accounts, where settlement / good-faith
rules apply instead).

Events are (symbol, side, exchange session). The daemon's day-state provider builds them from
the durable orders — every order that may have executed, bucketed by the session it was sent
in — and supplies the calendar-derived window start; the gate's ``rules.pattern_day_trader``
evaluates it. Counting is conservative: ``min(buys, sells)`` per symbol and session (no lot
matching) may count a round trip FINRA would not (e.g. selling an overnight position and
buying it back the same day [VERIFY]), never fewer. A broker-reported day-trade count (it
also sees trades made outside this system) is a floor.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import Order
from trader.core.enums import Side
from trader.risk.rules import RuleResult


@dataclass(frozen=True)
class TradeEvent:
    """One order that (may have) executed: its symbol, side and exchange session."""

    symbol: str
    side: Side
    session: date


class PDTRule:
    def __init__(self, config: RiskConfig) -> None:
        self._cfg = config

    def count_day_trades(self, events: Sequence[TradeEvent], *, window_start: date) -> int:
        """Number of day-trades in sessions on or after ``window_start``.

        A day-trade is a buy+sell round trip in one symbol in one session; multiple round
        trips in the same symbol/session each count (approximated as ``min(buys, sells)`` —
        conservative without lot matching), so the rule never under-counts toward the limit."""
        sessions: dict[tuple[str, date], list[int]] = {}  # (symbol, session) -> [buys, sells]
        for e in events:
            if e.session < window_start:
                continue
            cell = sessions.setdefault((e.symbol, e.session), [0, 0])
            cell[0 if e.side is Side.BUY else 1] += 1
        return sum(min(buys, sells) for buys, sells in sessions.values())

    @staticmethod
    def _completes_day_trade(order: Order, events: Sequence[TradeEvent], *, today: date) -> bool:
        """True if ``order`` closes a position opened (opposite side) in the SAME session —
        i.e. it would complete a new round-trip day-trade."""
        opposite = Side.SELL if order.side is Side.BUY else Side.BUY
        return any(
            e.symbol == order.symbol and e.session == today and e.side is opposite for e in events
        )

    def check(
        self,
        order: Order,
        *,
        events: Sequence[TradeEvent],
        equity: Decimal,
        today: date,
        window_start: date,
        opens_position: bool = False,
        broker_day_trades: int | None = None,
    ) -> RuleResult:
        """While equity is under the threshold (and enforcement is on): at the limit, block the
        order that would complete another day-trade, and any order that opens or adds to a
        position (``opens_position``)."""
        if not self._cfg.enforce_pdt:
            return RuleResult(ok=True)
        threshold = self._cfg.pdt_equity_threshold_usd
        if equity >= threshold:
            return RuleResult(ok=True)  # PDT only applies under the equity threshold
        count = max(
            self.count_day_trades(events, window_start=window_start), broker_day_trades or 0
        )
        limit = self._cfg.pdt_max_day_trades
        if count < limit:
            return RuleResult(ok=True)
        if self._completes_day_trade(order, events, today=today):
            return RuleResult(
                ok=False,
                reason=(
                    f"PDT: {count} day-trades in window; another is blocked while equity "
                    f"< {threshold}"
                ),
            )
        if opens_position:
            return RuleResult(
                ok=False,
                reason=(
                    f"PDT: at the limit of {limit} day-trades while equity < {threshold}; a new "
                    "position could not be closed this session"
                ),
            )
        return RuleResult(ok=True)


__all__ = ["PDTRule", "TradeEvent"]
