"""Vectorized parameter-sweep for OFFLINE research (design Appendix A, M6.9).

Loads cached daily bars (read-only Parquet) and runs a pandas/numpy-vectorized
mean-reversion approximation across a strategy parameter grid, producing a results table
ranking the combinations by total_return / max_drawdown / hit_rate / num_trades.

IMPORTANT — this is an APPROXIMATION, not the parity path:
  * It computes a clean trailing z-score (``rolling(lookback)`` mean/std, ddof=1) and a
    simple shifted-return P&L; it does NOT reproduce the event-driven backtest's
    asof/quote-in-window semantics, fees, sizing, or risk gate.
  * It exists to cheaply SHORTLIST parameters. Promising combos MUST be re-validated
    through the real event-driven backtest (``trader backtest`` / M6.7) before any use.

Isolation (load-bearing safety): this module imports ONLY pandas/numpy/stdlib — no
``trader.*`` module, so it can never construct an Order/Broker or touch the network.
Prices are read as float here (speed over exactness) — acceptable for ranking only.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Strategy families this harness can approximate (kept tiny + explicit).
SUPPORTED_STRATEGIES = ("zscore_revert",)

RESULT_COLUMNS = ("total_return", "max_drawdown", "hit_rate", "num_trades", "num_symbols")


# --------------------------------------------------------------------------- #
# Data loading (read-only; never fetches)                                      #
# --------------------------------------------------------------------------- #


def available_symbols(data_cache: str | Path) -> list[str]:
    """Symbols present in the cache (sorted). Empty if the cache has none."""
    bars_dir = Path(data_cache) / "bars"
    if not bars_dir.is_dir():
        return []
    return sorted(p.name for p in bars_dir.iterdir() if p.is_dir())


def load_cached_bars(
    data_cache: str | Path,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Read a symbol's cached daily closes as a ``ts``/``close`` (float) frame, ascending.

    Reads the same on-disk layout ``ParquetCache`` writes (``<root>/bars/<symbol>/*.parquet``)
    directly with pandas — NO import of ``trader.data`` (which would pull the live Schwab
    provider) and NO network. Returns an empty frame for a missing/uncached symbol (never
    fetches)."""
    symbol_dir = Path(data_cache) / "bars" / symbol
    parts = sorted(symbol_dir.glob("*.parquet")) if symbol_dir.is_dir() else []
    if not parts:
        return pd.DataFrame({"ts": [], "close": []})
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    out = pd.DataFrame(
        {
            "ts": pd.to_datetime(frame["ts"], utc=True),
            "close": frame["close"].astype(float),  # research approximation: float, not Decimal
        }
    ).sort_values("ts")
    if start is not None:
        out = out[out["ts"] >= pd.Timestamp(datetime(start.year, start.month, start.day), tz="UTC")]
    if end is not None:
        # inclusive end-of-day
        out = out[out["ts"] <= pd.Timestamp(datetime(end.year, end.month, end.day), tz="UTC")]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Vectorized mean-reversion approximation                                      #
# --------------------------------------------------------------------------- #


def zscore_positions(
    close: pd.Series, *, lookback: int, z_entry: float, z_exit: float
) -> pd.Series:
    """Target position series (1 long / -1 short / 0 flat) from a trailing z-score.

    Enter long when z<=-z_entry, short when z>=z_entry; flatten when |z|<=z_exit; otherwise
    hold the prior position (forward-filled). Vectorized approximation of the mean-reversion
    rule — NOT the event-driven flat-gating."""
    mean = close.rolling(lookback).mean()
    std = close.rolling(lookback).std(ddof=1)
    z = (close - mean) / std
    raw = pd.Series(np.nan, index=close.index, dtype="float64")
    raw[z <= -z_entry] = 1.0
    raw[z >= z_entry] = -1.0
    raw[z.abs() <= z_exit] = 0.0  # exit band wins (reverts toward the mean)
    return raw.ffill().fillna(0.0)


def _segment_metrics(strat_returns: pd.Series, positions: pd.Series) -> tuple[int, float]:
    """(num_trades, hit_rate) over holding segments.

    A holding segment is a maximal run where the position is a constant nonzero value (a
    long->short flip ends one segment and starts the next). num_trades counts every segment.
    Under the shifted-return convention (``strat_returns[i] = positions[i-1] * returns[i]``),
    a segment held over bars ``[s, e]`` earns its P&L on bars ``s+1 .. e+1``; each bar's
    return is credited to EXACTLY one segment (no flip-bar double counting). hit_rate is the
    fraction of CLOSED segments (those that end before the series ends) with positive realized
    return; a position still open at the last bar is excluded (mirrors metrics.hit_rate ->
    None when nothing closed)."""
    pos = positions.to_numpy()
    rets = strat_returns.fillna(0.0).to_numpy()
    n = len(pos)
    segments: list[tuple[int, int]] = []  # inclusive (start, end) of each constant-nonzero run
    i = 0
    while i < n:
        if pos[i] != 0.0:
            j = i
            while j + 1 < n and pos[j + 1] == pos[i]:
                j += 1
            segments.append((i, j))
            i = j + 1
        else:
            i += 1
    closed = 0
    wins = 0
    for start, end in segments:
        if end >= n - 1:
            continue  # still open at the last bar -> never closed -> excluded from hit_rate
        closed += 1
        if float(rets[start + 1 : end + 2].sum()) > 0:  # P&L on bars start+1 .. end+1
            wins += 1
    hit_rate = (wins / closed) if closed else math.nan
    return len(segments), hit_rate


def simulate(close: pd.Series, positions: pd.Series) -> dict[str, float]:
    """Run the approximation and return float metrics consistent with the M6.5 definitions
    (total_return, max_drawdown, hit_rate, num_trades)."""
    returns = close.pct_change()
    # Shift the position by one bar: a position taken at bar t earns bar t+1's return
    # (no-lookahead — you can't trade on a bar you haven't seen close yet).
    strat_returns = positions.shift(1) * returns
    equity = (1.0 + strat_returns.fillna(0.0)).cumprod()
    if equity.empty:
        return {"total_return": 0.0, "max_drawdown": 0.0, "hit_rate": math.nan, "num_trades": 0}
    total_return = float(equity.iloc[-1] - 1.0)
    running_peak = equity.cummax()
    drawdown = (running_peak - equity) / running_peak
    max_drawdown = float(drawdown.max())
    num_trades, hit_rate = _segment_metrics(strat_returns, positions)
    return {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "hit_rate": hit_rate,
        "num_trades": num_trades,
    }


# --------------------------------------------------------------------------- #
# Sweep                                                                        #
# --------------------------------------------------------------------------- #


def _grid_combos(param_grid: Mapping[str, Sequence[object]]) -> list[dict[str, object]]:
    """Cartesian product of the grid, in a deterministic (sorted-key) order."""
    if not param_grid:
        return []
    keys = sorted(param_grid)
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(param_grid[k] for k in keys))
    ]


def _combo_metrics(
    strategy: str, params: Mapping[str, object], bars_by_symbol: Mapping[str, pd.DataFrame]
) -> dict[str, float]:
    """Mean metrics for one param combo, averaged across symbols that have data."""
    per_symbol: list[dict[str, float]] = []
    for symbol in sorted(bars_by_symbol):
        df = bars_by_symbol[symbol]
        if df.empty:
            continue
        # Grid values arrive typed as ``object``; coerce via str (exact for int/float tokens).
        positions = zscore_positions(
            df["close"],
            lookback=int(str(params["lookback"])),
            z_entry=float(str(params["z_entry"])),
            z_exit=float(str(params.get("z_exit", 0.5))),
        )
        per_symbol.append(simulate(df["close"], positions))
    n = len(per_symbol)
    if n == 0:
        return {
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "hit_rate": math.nan,
            "num_trades": 0,
            "num_symbols": 0,
        }
    # Average hit_rate over symbols that actually closed a trade; NaN if none did
    # (avoids numpy's "Mean of empty slice" warning on an all-NaN list).
    hit_rates = [m["hit_rate"] for m in per_symbol if not math.isnan(m["hit_rate"])]
    return {
        "total_return": sum(m["total_return"] for m in per_symbol) / n,
        "max_drawdown": sum(m["max_drawdown"] for m in per_symbol) / n,
        "hit_rate": (sum(hit_rates) / len(hit_rates)) if hit_rates else math.nan,
        "num_trades": sum(m["num_trades"] for m in per_symbol),
        "num_symbols": n,
    }


def sweep(
    strategy: str,
    param_grid: Mapping[str, Sequence[object]],
    bars_by_symbol: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    """One row per parameter combination with ranking metrics. Deterministic (sorted keys +
    symbols, no RNG). An empty grid yields an empty DataFrame. Raises ValueError for an
    unsupported strategy family."""
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(
            f"unsupported research strategy {strategy!r}; supported: {SUPPORTED_STRATEGIES}"
        )
    combos = _grid_combos(param_grid)
    if not combos:
        return pd.DataFrame(columns=[*sorted(param_grid), *RESULT_COLUMNS])
    rows = [{**combo, **_combo_metrics(strategy, combo, bars_by_symbol)} for combo in combos]
    return pd.DataFrame(rows)


def load_bars_for_symbols(
    data_cache: str | Path,
    symbols: Sequence[str],
    start: date | None = None,
    end: date | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Load bars for each symbol; return (bars_by_symbol, missing_symbols). A symbol with no
    cached data is SKIPPED (recorded in missing) — never fetched."""
    bars: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for symbol in symbols:
        df = load_cached_bars(data_cache, symbol, start, end)
        if df.empty:
            missing.append(symbol)
        else:
            bars[symbol] = df
    return bars, missing


__all__ = [
    "RESULT_COLUMNS",
    "SUPPORTED_STRATEGIES",
    "available_symbols",
    "load_bars_for_symbols",
    "load_cached_bars",
    "simulate",
    "sweep",
    "zscore_positions",
]
