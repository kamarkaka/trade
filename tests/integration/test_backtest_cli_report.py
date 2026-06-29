"""End-to-end `trader backtest` over a small cached fixture (M6.7).

Runs TWO real strategies (threshold + zscore_revert) over a committed-style Parquet
fixture and asserts the per-strategy + combined report is written. Fully OFFLINE and
deterministic — no Schwab, no network, no broker (the read-only/no-real-money safe path
mandated pre-M5).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from trader.app.cli import app
from trader.data.cache import ParquetCache

runner = CliRunner()

# Mon..Fri trading week (no XNYS holiday); 07-05 seeds the first prev_close.
START = "2024-07-08"
END = "2024-07-12"


def _bars(closes: dict[str, str]) -> pd.DataFrame:
    rows = sorted(
        (datetime.fromisoformat(d).replace(tzinfo=UTC), Decimal(c)) for d, c in closes.items()
    )
    return pd.DataFrame(
        {
            "ts": [ts for ts, _ in rows],
            "open": [p for _, p in rows],
            "high": [p for _, p in rows],
            "low": [p for _, p in rows],
            "close": [p for _, p in rows],
            "volume": [10000 for _ in rows],
        }
    )


# AAPL declines ~3%/day so the threshold "momentum" strategy BUYs each session.
_AAPL = {
    "2024-07-05": "100",
    "2024-07-08": "97",
    "2024-07-09": "94",
    "2024-07-10": "91",
    "2024-07-11": "88",
    "2024-07-12": "85",
}
# MSFT wiggles around a mean to give the zscore "meanrev" strategy something to chew on.
_MSFT = {
    "2024-07-05": "200",
    "2024-07-08": "210",
    "2024-07-09": "190",
    "2024-07-10": "205",
    "2024-07-11": "188",
    "2024-07-12": "207",
}


def _write_config(path: Path, data_cache: Path) -> None:
    path.write_text(
        f"""
mode: backtest
schedule:
  base_seed: 12345
strategies:
  - id: momentum
    name: threshold
    params: {{ band: 0.02, lot: 10 }}
    universe: [AAPL]
    slots:
      - {{ id: open, time: "10:00", drift_max_minutes: 0 }}
  - id: meanrev
    name: zscore_revert
    params: {{ lookback: 3, z_entry: 1.0, z_exit: 0.5, lot: 10 }}
    universe: [MSFT]
    slots:
      - {{ id: mid, time: "11:00", drift_max_minutes: 0 }}
risk:
  allowlist: [AAPL, MSFT]
observability:
  data_cache: "{data_cache}"
""",
        encoding="utf-8",
    )


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    data_cache = tmp_path / "cache"
    cache = ParquetCache(data_cache)
    cache.write_bars("AAPL", _bars(_AAPL))
    cache.write_bars("MSFT", _bars(_MSFT))
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, data_cache)
    return cfg, data_cache


def _run(cfg: Path, out: Path):
    return runner.invoke(
        app,
        ["backtest", "--config", str(cfg), "--start", START, "--end", END, "--out-dir", str(out)],
    )


def test_backtest_writes_reports(tmp_path: Path) -> None:
    cfg, _ = _setup(tmp_path)
    out = tmp_path / "reports"
    result = _run(cfg, out)
    assert result.exit_code == 0, result.output
    assert len(list(out.glob("*/report.json"))) == 1
    assert len(list(out.glob("*/report.html"))) == 1
    assert len(list(out.glob("*/manifest.json"))) == 1


def test_report_has_both_strategies(tmp_path: Path) -> None:
    cfg, _ = _setup(tmp_path)
    out = tmp_path / "reports"
    assert _run(cfg, out).exit_code == 0
    report = json.loads(next(out.glob("*/report.json")).read_text())
    assert "combined" in report
    assert set(report["per_strategy"]) == {"momentum", "meanrev"}
    assert report["manifest"]["data_hashes"]["AAPL"]
    # momentum BUYs each declining session
    assert report["per_strategy"]["momentum"]["num_trades"] >= 1
    # combined num_trades reconciles to the sum across strategies
    total = sum(b["num_trades"] for b in report["per_strategy"].values())
    assert report["combined"]["num_trades"] == total


def test_stdout_summary_table(tmp_path: Path) -> None:
    cfg, _ = _setup(tmp_path)
    out = tmp_path / "reports"
    result = _run(cfg, out)
    assert result.exit_code == 0
    assert "COMBINED" in result.output
    assert "momentum" in result.output
    assert "meanrev" in result.output


def test_report_is_reproducible(tmp_path: Path) -> None:
    cfg, _ = _setup(tmp_path)
    assert _run(cfg, tmp_path / "r1").exit_code == 0
    assert _run(cfg, tmp_path / "r2").exit_code == 0
    a = next((tmp_path / "r1").glob("*/report.json")).read_text()
    b = next((tmp_path / "r2").glob("*/report.json")).read_text()
    assert a == b  # byte-identical report.json across runs (same machine)


def test_no_report_flags_skip_files(tmp_path: Path) -> None:
    cfg, _ = _setup(tmp_path)
    out = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "backtest",
            "--config",
            str(cfg),
            "--start",
            START,
            "--end",
            END,
            "--out-dir",
            str(out),
            "--no-report-html",
        ],
    )
    assert result.exit_code == 0
    assert len(list(out.glob("*/report.json"))) == 1
    assert list(out.glob("*/report.html")) == []  # suppressed


def test_empty_cache_warns_zero_fills(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, tmp_path / "empty_cache")  # no bars written
    out = tmp_path / "reports"
    result = _run(cfg, out)
    assert result.exit_code == 0
    assert "no fills produced" in result.output
    report = json.loads(next(out.glob("*/report.json")).read_text())
    assert report["combined"]["num_trades"] == 0


def test_rejects_reversed_range(tmp_path: Path) -> None:
    cfg, _ = _setup(tmp_path)
    result = runner.invoke(app, ["backtest", "--config", str(cfg), "--start", END, "--end", START])
    assert result.exit_code != 0
    assert "on or after" in result.output
