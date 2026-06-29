"""Deterministic numeric tests for the shared indicators (M6.2): exact Decimal values,
insufficient-data -> None (never raise), invalid window -> ValueError, no-lookahead slicing."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest

from trader.core import Bar
from trader.strategy import indicators as ind


def _d(*xs: str) -> list[Decimal]:
    return [Decimal(x) for x in xs]


def test_sma_known_values() -> None:
    assert ind.sma(_d("10", "20", "30"), 3) == Decimal(20)
    assert ind.sma(_d("10", "20"), 3) is None  # len < window
    assert ind.rolling_mean(_d("10", "20", "30", "40"), 2) == Decimal(35)  # last 2 only


def test_rolling_std_sample_ddof1() -> None:
    # [10,20,30]: mean 20, var=(100+0+100)/(3-1)=100, std=10
    assert ind.rolling_std(_d("10", "20", "30"), 3) == Decimal(10)
    assert ind.rolling_std(_d("5"), 1) is None  # window-ddof = 0 -> None


def test_zscore_known_values() -> None:
    assert ind.zscore(_d("10", "20", "30"), 3) == Decimal(1)  # (30-20)/10
    assert ind.zscore(_d("5", "5", "5"), 3) is None  # std == 0 -> None
    assert ind.zscore(_d("10", "20"), 3) is None  # insufficient data


def test_ema_seeded_by_sma() -> None:
    # len == window -> EMA is just the seed (SMA of the window)
    assert ind.ema(_d("10", "20", "30"), 3) == Decimal(20)
    # len > window: seed=SMA([10,20,30])=20, alpha=2/4=0.5, e=0.5*40+0.5*20=30
    assert ind.ema(_d("10", "20", "30", "40"), 3) == Decimal(30)
    assert ind.ema(_d("10", "20"), 3) is None


def test_simple_returns() -> None:
    assert ind.simple_returns(_d("10", "20", "30")) == [Decimal(1), Decimal("0.5")]
    with pytest.raises(ValueError, match="zero base"):
        ind.simple_returns(_d("0", "10"))


def test_insufficient_data_returns_none_not_raise() -> None:
    short = _d("1", "2")
    assert ind.sma(short, 5) is None
    assert ind.rolling_mean(short, 5) is None
    assert ind.rolling_std(short, 5) is None
    assert ind.zscore(short, 5) is None
    assert ind.ema(short, 5) is None


def test_invalid_window_raises() -> None:
    for fn in (ind.sma, ind.rolling_mean, ind.rolling_std, ind.zscore, ind.ema):
        with pytest.raises(ValueError, match="window must be positive"):
            fn(_d("1", "2", "3"), 0)
        with pytest.raises(ValueError, match="window must be positive"):
            fn(_d("1", "2", "3"), -1)


def test_nan_rejected() -> None:
    with pytest.raises(ValueError, match="NaN"):
        ind.sma([Decimal(1), Decimal("nan"), Decimal(3)], 3)


def test_uses_last_window_only_no_lookahead() -> None:
    # Older values outside the trailing window must not affect the result.
    assert ind.rolling_mean(_d("999", "10", "20", "30"), 3) == Decimal(20)


def test_rolling_std_non_perfect_square_exact() -> None:
    # sqrt(3) at the pinned 28-digit context — pins the deterministic Decimal value.
    assert ind.rolling_std(_d("0", "0", "3"), 3) == Decimal("1.732050807568877293527446342")


def test_rolling_std_population_ddof0() -> None:
    # population variance = 200/3; std = sqrt(200/3)
    expected = (Decimal(200) / Decimal(3)).sqrt()
    assert ind.rolling_std(_d("10", "20", "30"), 3, ddof=0) == expected


def test_ema_multi_step_recursion() -> None:
    # window 2: seed SMA([2,4])=3, alpha=2/3; e=2/3*6+1/3*3=5; then e=2/3*8+1/3*5=7
    assert ind.ema(_d("2", "4", "6", "8"), 2) == Decimal(7)


def test_indicators_invariant_to_ambient_context() -> None:
    # Output must NOT depend on the (mutable, global) ambient Decimal context.
    expected = ind.rolling_std(_d("0", "0", "3"), 3)
    with localcontext() as ctx:
        ctx.prec = 5  # a hostile, low-precision ambient context
        assert ind.rolling_std(_d("0", "0", "3"), 3) == expected
        assert ind.zscore(_d("10", "20", "30"), 3) == Decimal(1)


def test_closes_from_bars() -> None:
    bars = [
        Bar(
            "AAPL",
            datetime(2026, 6, i + 1, tzinfo=UTC),
            Decimal(1),
            Decimal(2),
            Decimal(0),
            Decimal(c),
            100,
        )
        for i, c in enumerate(("100", "101", "102"))
    ]
    assert ind.closes_from_bars(bars) == _d("100", "101", "102")
