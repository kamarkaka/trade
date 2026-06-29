"""End-to-end `trader backtest` over a small cached fixture (M2.11)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from trader.app.cli import app
from trader.data.cache import ParquetCache

runner = CliRunner()


def _bars() -> pd.DataFrame:
    rows = [(datetime(2023, 1, d, tzinfo=UTC), Decimal(f"{100 + d}")) for d in range(2, 7)]
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


def _write_config(path: Path, data_cache: Path) -> None:
    path.write_text(
        f"""
mode: backtest
strategies:
  - id: s1
    name: buyhold
    universe: [AAPL]
    slots:
      - {{id: open, time: "15:00"}}
observability:
  data_cache: "{data_cache}"
""",
        encoding="utf-8",
    )


def test_backtest_cli_produces_report(tmp_path: Path) -> None:
    data_cache = tmp_path / "cache"
    ParquetCache(data_cache).write_bars("AAPL", _bars())
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, data_cache)
    out = tmp_path / "reports"

    result = runner.invoke(
        app,
        [
            "backtest",
            "--config",
            str(cfg),
            "--start",
            "2023-01-02",
            "--end",
            "2023-01-06",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output

    report_files = list(out.glob("*/report.json"))
    manifest_files = list(out.glob("*/manifest.json"))
    assert len(report_files) == 1
    assert len(manifest_files) == 1

    report = json.loads(report_files[0].read_text())
    assert report["blotter"], "expected non-empty trade blotter"
    assert report["equity_curve"], "expected non-empty equity curve"
    assert report["manifest"]["data_hashes"]["AAPL"]
    assert report["blotter"][0]["symbol"] == "AAPL"


def test_backtest_cli_rejects_reversed_range(tmp_path: Path) -> None:
    data_cache = tmp_path / "cache"
    ParquetCache(data_cache).write_bars("AAPL", _bars())
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, data_cache)
    result = runner.invoke(
        app,
        ["backtest", "--config", str(cfg), "--start", "2023-01-06", "--end", "2023-01-02"],
    )
    assert result.exit_code != 0
    assert "on or after" in result.output
