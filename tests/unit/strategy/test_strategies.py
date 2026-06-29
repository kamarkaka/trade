"""Tests for the StrategyRegistry + the two stub strategies (M3.6)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import trader.strategy  # noqa: F401 - registers built-ins
from fakes import FakeClock, FakeMarketDataProvider
from trader.core import Account, Bar, MarketSnapshot, Quote
from trader.core.enums import Action
from trader.strategy.registry import REGISTRY
from trader.strategy.strategies.threshold import ThresholdStrategy

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)
ACCOUNT = Account(cash=Decimal("100000"), buying_power=Decimal("100000"), equity=Decimal("100000"))


def _quote(symbol: str, last: str, prev_close: str | None) -> Quote:
    p = Decimal(last)
    return Quote(
        symbol=symbol,
        ts=NOW,
        last=p,
        bid=p,
        ask=p,
        volume=1000,
        prev_close=Decimal(prev_close) if prev_close is not None else None,
    )


def _snapshot(*quotes: Quote) -> MarketSnapshot:
    return MarketSnapshot(asof=NOW, quotes={q.symbol: q for q in quotes})


# --- registry --------------------------------------------------------------- #


def test_registry_create() -> None:
    strat = REGISTRY.create("threshold", {"lot": 5})
    assert isinstance(strat, ThresholdStrategy)
    assert strat.lot == 5
    assert "threshold" in REGISTRY.names() and "zscore_revert" in REGISTRY.names()


def test_registry_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown strategy"):
        REGISTRY.create("nope", {})


# --- threshold -------------------------------------------------------------- #


def _threshold_decisions(quote: Quote, band: float = 0.02):
    strat = REGISTRY.create("threshold", {"band": band, "lot": 10})
    return strat.decide(_snapshot(quote), [], ACCOUNT, FakeMarketDataProvider(), FakeClock(NOW))


def test_threshold_buy_dip() -> None:
    decisions = _threshold_decisions(_quote("AAPL", "97", "100"))  # 97 < 100*0.98
    assert [(d.action, d.quantity) for d in decisions] == [(Action.BUY, 10)]


def test_threshold_sell_pop() -> None:
    decisions = _threshold_decisions(_quote("AAPL", "103", "100"))  # 103 > 100*1.02
    assert decisions[0].action is Action.SELL


def test_threshold_holds_within_band() -> None:
    assert _threshold_decisions(_quote("AAPL", "100", "100")) == []  # no signal


def test_threshold_skips_missing_prev_close() -> None:
    assert _threshold_decisions(_quote("AAPL", "97", None)) == []


def test_threshold_exact_band_edge_holds() -> None:
    # last == prev_close*(1-band) exactly is NOT below it (strict <) -> hold
    assert _threshold_decisions(_quote("AAPL", "98", "100")) == []


def test_threshold_multi_symbol() -> None:
    strat = REGISTRY.create("threshold", {"band": 0.02, "lot": 10})
    snap = _snapshot(_quote("AAPL", "97", "100"), _quote("MSFT", "103", "100"))
    decisions = strat.decide(snap, [], ACCOUNT, FakeMarketDataProvider(), FakeClock(NOW))
    by_symbol = {d.symbol: d.action for d in decisions}
    assert by_symbol == {"AAPL": Action.BUY, "MSFT": Action.SELL}


# --- zscore_revert ---------------------------------------------------------- #


def _bars(symbol: str, closes: list[str]) -> list[Bar]:
    bars = []
    n = len(closes)
    for i, c in enumerate(closes):
        p = Decimal(c)
        ts = NOW - timedelta(days=n - i)  # consecutive days ending just before asof
        bars.append(Bar(symbol=symbol, ts=ts, open=p, high=p, low=p, close=p, volume=1000))
    return bars


def _zscore_decisions(last: str, closes: list[str], *, lookback: int = 3):
    data = FakeMarketDataProvider(bars={"AAPL": _bars("AAPL", closes)})
    strat = REGISTRY.create("zscore_revert", {"lookback": lookback, "z_entry": 2.0, "lot": 10})
    return strat.decide(_snapshot(_quote("AAPL", last, "100")), [], ACCOUNT, data, FakeClock(NOW))


def test_zscore_buy_oversold() -> None:
    decisions = _zscore_decisions("90", ["100", "101", "99"])  # mean~100, std~0.8, z<<-2
    assert decisions[0].action is Action.BUY


def test_zscore_sell_overbought() -> None:
    decisions = _zscore_decisions("110", ["100", "101", "99"])
    assert decisions[0].action is Action.SELL


def test_zscore_zero_std_holds() -> None:
    assert _zscore_decisions("90", ["100", "100", "100"]) == []  # std==0 -> no signal, no error


def test_zscore_insufficient_bars_holds() -> None:
    assert _zscore_decisions("90", ["100", "101"], lookback=3) == []  # < lookback bars
