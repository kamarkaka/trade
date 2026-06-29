"""Tests for the OFFLINE research param-sweep (M6.9).

The load-bearing test is ``test_research_imports_no_broker``: a subprocess import +
sys.modules scan proving the research package can never reach a broker / network path
(stronger than a static scan — catches transitive + lazy imports).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trader.research import param_sweep

FORBIDDEN = (
    "trader.broker",
    "trader.schwab",
    "trader.auth",
    "trader.execution",
    "trader.orchestrator",
)


def _close_series(n: int = 40) -> pd.DataFrame:
    # Deterministic oscillation around 100 so the z-score crosses entry/exit bands.
    closes = [100.0 + (10.0 if i % 4 == 0 else -8.0 if i % 4 == 2 else 0.0) for i in range(n)]
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"ts": ts, "close": closes})


def test_sweep_produces_grid() -> None:
    bars = {"AAA": _close_series(), "BBB": _close_series()}
    grid = {"lookback": [3, 5], "z_entry": [1.0, 2.0]}
    out = param_sweep.sweep("zscore_revert", grid, bars)
    assert len(out) == 4  # 2x2 grid
    for col in ("total_return", "max_drawdown", "hit_rate", "num_trades", "num_symbols"):
        assert col in out.columns
    assert set(out["lookback"]) == {3, 5}
    assert (out["num_symbols"] == 2).all()


def test_sweep_is_deterministic() -> None:
    bars = {"AAA": _close_series()}
    grid = {"lookback": [3, 5], "z_entry": [1.0, 1.5]}
    a = param_sweep.sweep("zscore_revert", grid, bars)
    b = param_sweep.sweep("zscore_revert", grid, bars)
    assert a.to_csv(index=False) == b.to_csv(index=False)


def test_empty_grid_yields_empty_frame() -> None:
    out = param_sweep.sweep("zscore_revert", {}, {"AAA": _close_series()})
    assert out.empty


def test_unsupported_strategy_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported research strategy"):
        param_sweep.sweep("nope", {"lookback": [3]}, {"AAA": _close_series()})


def test_missing_symbol_does_not_fetch(tmp_path: Path) -> None:
    # Write only AAA to the cache, request AAA + MSG -> MSG skipped, never fetched.
    from trader.data.cache import ParquetCache

    cache = ParquetCache(tmp_path)
    df = _close_series(10)
    cache.write_bars(
        "AAA",
        pd.DataFrame(
            {
                "ts": df["ts"],
                "open": [Decimal(str(c)) for c in df["close"]],
                "high": [Decimal(str(c)) for c in df["close"]],
                "low": [Decimal(str(c)) for c in df["close"]],
                "close": [Decimal(str(c)) for c in df["close"]],
                "volume": [1000] * len(df),
            }
        ),
    )
    bars, missing = param_sweep.load_bars_for_symbols(tmp_path, ["AAA", "MSG"], None, None)
    assert "AAA" in bars and bars["AAA"].shape[0] == 10
    assert missing == ["MSG"]
    # A missing symbol returns an empty frame (no exception, no fetch).
    assert param_sweep.load_cached_bars(tmp_path, "MSG").empty


def test_available_symbols(tmp_path: Path) -> None:
    assert param_sweep.available_symbols(tmp_path) == []  # empty cache
    assert param_sweep.available_symbols(tmp_path / "nope") == []  # missing dir


def test_load_respects_date_window(tmp_path: Path) -> None:
    from trader.data.cache import ParquetCache

    cache = ParquetCache(tmp_path)
    df = _close_series(20)
    cache.write_bars(
        "AAA",
        pd.DataFrame(
            {
                "ts": df["ts"],
                "open": [Decimal(str(c)) for c in df["close"]],
                "high": [Decimal(str(c)) for c in df["close"]],
                "low": [Decimal(str(c)) for c in df["close"]],
                "close": [Decimal(str(c)) for c in df["close"]],
                "volume": [1000] * len(df),
            }
        ),
    )
    windowed = param_sweep.load_cached_bars(tmp_path, "AAA", date(2024, 1, 5), date(2024, 1, 10))
    assert (windowed["ts"].dt.day >= 5).all() and (windowed["ts"].dt.day <= 10).all()


def test_research_imports_no_broker() -> None:
    """Subprocess import + sys.modules scan: importing the research package AND running a
    sweep must pull in NO broker/schwab/auth/execution/orchestrator module (transitive or
    lazy). This is the structural guarantee that research can never trade."""
    probe = (
        "import sys\n"
        "import trader.research\n"
        "from trader.research import param_sweep\n"
        "import pandas as pd\n"
        "df = pd.DataFrame({'ts': pd.date_range('2024-01-01', periods=20, freq='D', tz='UTC'),\n"
        "                   'close': [100.0 + (i % 5) for i in range(20)]})\n"
        "param_sweep.sweep('zscore_revert', {'lookback':[3], 'z_entry':[1.0]}, {'AAA': df})\n"
        "forbidden = sorted(m for m in sys.modules if m.startswith("
        f"{FORBIDDEN!r}))\n"
        "print(';'.join(forbidden))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = [m for m in result.stdout.strip().split(";") if m]
    assert leaked == [], f"research pulled in forbidden modules: {leaked}"
