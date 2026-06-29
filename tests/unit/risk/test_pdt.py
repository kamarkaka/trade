"""PDT rule: day-trade counting, rolling-window expiry, the under-$25k 4th-trade block,
the over-threshold allowance, and the enforce_pdt disable flag (M5.5)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import Order
from trader.core.enums import OrderType, Side
from trader.risk.pdt import PDTRule, TradeEvent

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)  # a Monday session
WINDOW_START = date(2026, 6, 23)  # 5 sessions back (caller-supplied)


def _events(n: int, *, day: datetime = NOW) -> list[TradeEvent]:
    # n distinct same-session round trips (buy+sell of a unique symbol each).
    out: list[TradeEvent] = []
    for i in range(n):
        sym = f"S{i}"
        out.append(TradeEvent(sym, Side.BUY, day))
        out.append(TradeEvent(sym, Side.SELL, day))
    return out


def _order(side: Side = Side.SELL, symbol: str = "AAPL") -> Order:
    return Order("c1", "s1", symbol, side, 10, OrderType.MARKET)


def _under() -> Decimal:
    return Decimal("10000")  # below the 25k threshold


def test_count_day_trades() -> None:
    rule = PDTRule(RiskConfig())
    assert rule.count_day_trades(_events(3), window_start=WINDOW_START) == 3
    # a symbol with only a buy (no sell) is not a day-trade
    one_sided = [TradeEvent("X", Side.BUY, NOW)]
    assert rule.count_day_trades(one_sided, window_start=WINDOW_START) == 0


def test_session_bucketed_by_exchange_tz_not_utc() -> None:
    # A same-session ET round trip late in the afternoon crosses into the next UTC day; it
    # must still count as ONE day-trade (bucket by exchange-tz date, not UTC).
    rule = PDTRule(RiskConfig())
    buy = datetime(2026, 6, 29, 18, 0, tzinfo=UTC)  # 14:00 ET
    sell = datetime(2026, 6, 30, 0, 30, tzinfo=UTC)  # 20:30 ET SAME session (still 2026-06-29 ET)
    events = [TradeEvent("AAPL", Side.BUY, buy), TradeEvent("AAPL", Side.SELL, sell)]
    assert (
        rule.count_day_trades(events, window_start=WINDOW_START) == 1
    )  # not split across UTC days


def test_multiple_round_trips_same_session_count_each() -> None:
    # 3 buy+sell pairs in one symbol/session = 3 day-trades (not 1) -> never under-counts.
    rule = PDTRule(RiskConfig())
    events = [TradeEvent("AAPL", Side.BUY if i % 2 == 0 else Side.SELL, NOW) for i in range(6)]
    assert rule.count_day_trades(events, window_start=WINDOW_START) == 3


def test_short_side_day_trade_blocked() -> None:
    # Short-then-cover: a SELL opens the position today, a BUY now completes the round trip.
    rule = PDTRule(RiskConfig())
    events = [*_events(3), TradeEvent("AAPL", Side.SELL, NOW)]
    result = rule.check(
        _order(Side.BUY, "AAPL"),
        events=events,
        equity=_under(),
        asof=NOW,
        window_start=WINDOW_START,
    )
    assert result.ok is False


def test_rolling_window_expiry() -> None:
    rule = PDTRule(RiskConfig())
    old = _events(3, day=NOW - timedelta(days=30))  # well before the window
    assert rule.count_day_trades(old, window_start=WINDOW_START) == 0  # expired out of window


def test_blocks_fourth_day_trade_under_25k() -> None:
    rule = PDTRule(RiskConfig())  # max_day_trades=3, threshold=25k, enforce_pdt=True
    # 3 day-trades already this window; AAPL was bought today -> a SELL now completes the 4th.
    events = [*_events(3), TradeEvent("AAPL", Side.BUY, NOW)]
    result = rule.check(
        _order(Side.SELL, "AAPL"),
        events=events,
        equity=_under(),
        asof=NOW,
        window_start=WINDOW_START,
    )
    assert result.ok is False and "PDT" in result.reason


def test_allows_when_equity_over_25k() -> None:
    rule = PDTRule(RiskConfig())
    events = [*_events(3), TradeEvent("AAPL", Side.BUY, NOW)]
    result = rule.check(
        _order(Side.SELL, "AAPL"),
        events=events,
        equity=Decimal("25000"),  # at/over threshold -> PDT does not apply
        asof=NOW,
        window_start=WINDOW_START,
    )
    assert result.ok is True


def test_allows_when_not_completing_a_day_trade() -> None:
    rule = PDTRule(RiskConfig())
    # 3 day-trades in window, but AAPL was NOT opened today, so a BUY now just opens a
    # position (no same-session round trip completed) -> allowed.
    result = rule.check(
        _order(Side.BUY, "AAPL"),
        events=_events(3),
        equity=_under(),
        asof=NOW,
        window_start=WINDOW_START,
    )
    assert result.ok is True


def test_disabled_when_enforce_pdt_false() -> None:
    rule = PDTRule(RiskConfig(enforce_pdt=False))
    events = [*_events(5), TradeEvent("AAPL", Side.BUY, NOW)]
    result = rule.check(
        _order(Side.SELL, "AAPL"),
        events=events,
        equity=_under(),
        asof=NOW,
        window_start=WINDOW_START,
    )
    assert result.ok is True  # cash account / disabled -> never blocks


def test_configurable_max_day_trades() -> None:
    rule = PDTRule(RiskConfig(pdt_max_day_trades=1))  # stricter
    events = [TradeEvent("AAPL", Side.BUY, NOW)]  # 0 completed day-trades, but limit is 1...
    # with max=1, the FIRST completing trade is allowed (count 0 < 1); make count reach 1:
    events2 = [*_events(1), TradeEvent("AAPL", Side.BUY, NOW)]  # 1 completed + AAPL opened
    assert (
        rule.check(
            _order(Side.SELL, "AAPL"),
            events=events2,
            equity=_under(),
            asof=NOW,
            window_start=WINDOW_START,
        ).ok
        is False
    )
    assert (
        rule.check(
            _order(Side.SELL, "AAPL"),
            events=events,
            equity=_under(),
            asof=NOW,
            window_start=WINDOW_START,
        ).ok
        is True
    )  # only AAPL opened, count 0 < 1
