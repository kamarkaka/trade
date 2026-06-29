"""Synthetic-data tests for the real zscore_revert mean-reversion strategy (M6.4): BUY
oversold, SELL overbought, HOLD inside the band, exit-to-flat on reversion, and HOLD on
insufficient bars. No network / no real clock (FakeClock + FakeMarketDataProvider)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fakes import FakeClock, FakeMarketDataProvider
from trader.core import Account, Bar, MarketSnapshot, Position, Quote
from trader.core.enums import Action
from trader.strategy.params import ZScoreRevertParams
from trader.strategy.registry import REGISTRY

ASOF = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)
ACCOUNT = Account(cash=Decimal("100000"), buying_power=Decimal("100000"), equity=Decimal("100000"))
# lookback 3, z_entry 1.0, z_exit 0.5 -> window = [*closes, last] last 3; easy to hand-verify.
PARAMS = {"lookback": 3, "z_entry": 1.0, "z_exit": 0.5, "lot": 10}


def _strategy():
    return REGISTRY.create("zscore_revert", dict(PARAMS))


def _quote(last: str) -> Quote:
    return Quote("AAPL", ASOF, Decimal(last), Decimal("99"), Decimal("101"), 1000)


def _bars(closes: list[str]) -> FakeMarketDataProvider:
    bars = [
        Bar(
            "AAPL",
            ASOF - timedelta(days=len(closes) - i),
            Decimal("1"),
            Decimal("2"),
            Decimal("0"),
            Decimal(c),
            1000,
        )
        for i, c in enumerate(closes)
    ]
    return FakeMarketDataProvider(quotes={"AAPL": [_quote("0")]}, bars={"AAPL": bars})


def _decide(positions: list[Position], closes: list[str], last: str):
    snap = MarketSnapshot(asof=ASOF, quotes={"AAPL": _quote(last)})
    return _strategy().decide(snap, positions, ACCOUNT, _bars(closes), FakeClock(ASOF))


def test_buy_when_oversold() -> None:
    # window [10,10,4]: mean 8, sample std sqrt(12)~3.46, z=(4-8)/3.46~-1.15 <= -1.0 -> BUY
    decisions = _decide([], ["10", "10"], "4")
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action is Action.BUY and d.symbol == "AAPL" and d.quantity == 10


def test_sell_when_overbought() -> None:
    # window [10,10,16]: z=+1.15 >= 1.0 and flat -> SELL
    decisions = _decide([], ["10", "10"], "16")
    assert (
        len(decisions) == 1 and decisions[0].action is Action.SELL and decisions[0].quantity == 10
    )


def test_hold_inside_band() -> None:
    # window [10,12,11]: mean 11, std 1, z=0 -> |z| < z_entry and flat -> HOLD (no decision)
    assert _decide([], ["10", "12"], "11") == []


def test_exit_toward_flat_when_reverted() -> None:
    # holding +10, z=0 inside the exit band -> close the long (SELL 10)
    held = [Position("AAPL", 10, Decimal("100"), Decimal("1000"))]
    decisions = _decide(held, ["10", "12"], "11")
    assert (
        len(decisions) == 1 and decisions[0].action is Action.SELL and decisions[0].quantity == 10
    )


def test_exit_closes_short() -> None:
    held = [Position("AAPL", -10, Decimal("100"), Decimal("-1000"))]
    decisions = _decide(held, ["10", "12"], "11")
    assert decisions[0].action is Action.BUY and decisions[0].quantity == 10  # cover the short


def test_insufficient_bars_holds() -> None:
    # only 1 close + last => 2 values < lookback 3 -> z None -> HOLD
    assert _decide([], ["10"], "4") == []


def test_no_entry_when_already_holding() -> None:
    # oversold but already long -> no new BUY (and not inside the exit band) -> HOLD
    held = [Position("AAPL", 10, Decimal("100"), Decimal("1000"))]
    assert _decide(held, ["10", "10"], "4") == []


def test_params_validation() -> None:
    assert ZScoreRevertParams(lookback=5).lookback == 5
    import pytest

    with pytest.raises(Exception, match="lookback"):
        ZScoreRevertParams(lookback=1)  # ge=2
    with pytest.raises(Exception, match="extra"):
        ZScoreRevertParams(bogus=1)  # extra=forbid
