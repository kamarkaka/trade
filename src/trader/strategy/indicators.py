"""Pure, deterministic, no-lookahead indicators (design §6/§9.5).

The single source of truth for strategy math, shared by production strategies and the offline
research harness (M6.9) so they compute identically (parity). All functions:

- are pure (no global state, no wall clock, no I/O) and **Decimal**-based (no binary float;
  ``std`` uses ``Decimal.sqrt`` for cross-platform determinism);
- operate ONLY on the slice they are given. **NO-LOOKAHEAD CONTRACT:** the caller must pass
  only ``ts <= asof`` rows (the MarketDataProvider guarantees this, Appendix B). These
  functions never reorder, shift, or peek beyond the provided sequence — the most recent value
  is ``values[-1]``;
- return ``None`` (never raise, never pad with future data) on insufficient data;
- raise ``ValueError`` only on caller error: ``window <= 0`` or NaN/None inputs.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from decimal import Decimal

from trader.core import Bar


def _check_window(window: int) -> None:
    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")


def _validate(values: Sequence[Decimal]) -> None:
    for v in values:
        if v is None or not isinstance(v, Decimal):
            raise ValueError(f"values must be Decimal, got {v!r}")
        if v.is_nan():
            raise ValueError("values must not contain NaN")


def rolling_mean(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Mean of the last ``window`` values, or None if there aren't that many."""
    _check_window(window)
    _validate(values)
    if len(values) < window:
        return None
    return sum(values[-window:], Decimal(0)) / Decimal(window)


def sma(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Simple moving average of the last ``window`` values (alias of rolling_mean)."""
    return rolling_mean(values, window)


def rolling_std(values: Sequence[Decimal], window: int, ddof: int = 1) -> Decimal | None:
    """Sample (ddof=1) standard deviation of the last ``window`` values via Decimal.sqrt.
    None if there are fewer than ``window`` values or the divisor (window-ddof) is <= 0."""
    _check_window(window)
    _validate(values)
    if len(values) < window:
        return None
    denom = window - ddof
    if denom <= 0:
        return None  # not enough degrees of freedom (e.g. window=1, ddof=1)
    w = values[-window:]
    mean = sum(w, Decimal(0)) / Decimal(window)
    variance = sum(((x - mean) ** 2 for x in w), Decimal(0)) / Decimal(denom)
    return variance.sqrt()


def zscore(values: Sequence[Decimal], window: int) -> Decimal | None:
    """(last - rolling_mean) / rolling_std over ``window``. None on insufficient data or
    when std == 0 (a flat series has no meaningful z)."""
    mean = rolling_mean(values, window)
    std = rolling_std(values, window)
    if mean is None or std is None or std == 0:
        return None
    return (values[-1] - mean) / std


def ema(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Exponential moving average, alpha = 2/(window+1), seeded by the SMA of the first
    ``window`` values. None if there are fewer than ``window`` values."""
    _check_window(window)
    _validate(values)
    if len(values) < window:
        return None
    alpha = Decimal(2) / Decimal(window + 1)
    e = sum(values[:window], Decimal(0)) / Decimal(window)  # seed = SMA of the first window
    for v in values[window:]:
        e = alpha * v + (Decimal(1) - alpha) * e
    return e


def simple_returns(values: Sequence[Decimal]) -> list[Decimal]:
    """Period-over-period simple returns (v[i]-v[i-1])/v[i-1]. Raises if a base value is 0."""
    _validate(values)
    out: list[Decimal] = []
    for prev, cur in itertools.pairwise(values):
        if prev == 0:
            raise ValueError("cannot compute a return from a zero base value")
        out.append((cur - prev) / prev)
    return out


def closes_from_bars(bars: Sequence[Bar]) -> list[Decimal]:
    """Extract close prices (bars must already be ascending by ts and asof-filtered upstream)."""
    return [b.close for b in bars]


__all__ = [
    "closes_from_bars",
    "ema",
    "rolling_mean",
    "rolling_std",
    "simple_returns",
    "sma",
    "zscore",
]
