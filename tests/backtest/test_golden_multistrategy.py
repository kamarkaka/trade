"""Golden-run reproducibility for the multi-strategy backtest report (M6.8).

Runs the FULL per-strategy + combined backtest pipeline (the same ``run_backtest_report``
the ``trader backtest`` CLI uses) twice in-process over a fixed config + in-code bar
fixture + fixed base_seed, and asserts:
  * the two runs are byte-identical (intra-run determinism), and
  * the run matches the committed golden ``report_two_strats.json`` after stripping the
    environment-volatile manifest fields (git_commit / lib_versions / python_version).

This guards the whole M6 exit criterion against accidental lookahead / non-determinism
(seeded jitter, asof clamp, per-strategy attribution). Entirely OFFLINE — no Schwab, no
network, no real orders (pre-M5 safety).

The data fixture is defined IN CODE (not committed Parquet): ``ParquetCache.content_hash``
hashes a canonical row serialization, not the Parquet bytes, so the data_hash is identical
across machines / library versions when the bars are regenerated from this spec.

Regenerate the committed golden (after an intentional change) with:
    python tests/backtest/test_golden_multistrategy.py
"""

from __future__ import annotations

import difflib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from trader.backtest.report import strip_volatile
from trader.backtest.runner import run_backtest_report
from trader.config import load_config
from trader.data.cache import ParquetCache

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "golden_config.yaml"
GOLDEN = Path(__file__).parent / "golden" / "report_two_strats.json"
START = date(2024, 7, 1)
END = date(2024, 7, 12)


def _series(base: float, steps: list[float], first: date) -> dict[date, str]:
    """Daily closes from ``first`` (business days), each multiplied cumulatively by the
    next factor in ``steps`` (deterministic; quantized to cents)."""
    out: dict[date, str] = {}
    day = first
    price = Decimal(str(base))
    for factor in steps:
        while day.weekday() >= 5:  # skip Sat/Sun
            day += timedelta(days=1)
        price = (price * Decimal(str(factor))).quantize(Decimal("0.01"))
        out[day] = str(price)
        day += timedelta(days=1)
    return out


# AAPL declines ~3%/day from 2024-06-24 -> threshold "momentum" BUYs every session.
_AAPL = _series(100.0, [1.0] + [0.97] * 17, date(2024, 6, 24))
# MSFT: flat, a sharp -12% dip (oversold -> meanrev BUYs), then recovery (reverts -> exit).
_MSFT_FACTORS = [
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    0.88,
    1.0,
    1.08,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
]
_MSFT = _series(300.0, _MSFT_FACTORS, date(2024, 6, 24))


def _bars(closes: dict[date, str]) -> pd.DataFrame:
    rows = sorted(closes.items())
    return pd.DataFrame(
        {
            "ts": [datetime(d.year, d.month, d.day, tzinfo=UTC) for d, _ in rows],
            "open": [Decimal(c) for _, c in rows],
            "high": [Decimal(c) for _, c in rows],
            "low": [Decimal(c) for _, c in rows],
            "close": [Decimal(c) for _, c in rows],
            "volume": [100000 for _ in rows],
        }
    )


def _produce(cache_root: Path) -> dict[str, Any]:
    cache = ParquetCache(cache_root)
    cache.write_bars("AAPL", _bars(_AAPL))
    cache.write_bars("MSFT", _bars(_MSFT))
    cfg = load_config(
        FIXTURE_CONFIG, cli_overrides={"observability": {"data_cache": str(cache_root)}}
    )
    run = run_backtest_report(cfg, START, END)
    return strip_volatile(run.doc.data)


def test_intra_run_determinism(tmp_path: Path) -> None:
    first = _produce(tmp_path / "a")
    second = _produce(tmp_path / "b")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_matches_committed_golden(tmp_path: Path) -> None:
    produced = _produce(tmp_path / "run")
    golden = json.loads(GOLDEN.read_text())
    if produced != golden:  # unified diff to ease debugging on a mismatch
        diff = "\n".join(
            difflib.unified_diff(
                json.dumps(golden, indent=2, sort_keys=True).splitlines(),
                json.dumps(produced, indent=2, sort_keys=True).splitlines(),
                fromfile="golden",
                tofile="produced",
                lineterm="",
            )
        )
        raise AssertionError(f"report drifted from committed golden:\n{diff}")


def test_per_strategy_blocks_reproducible(tmp_path: Path) -> None:
    produced = _produce(tmp_path / "run")
    golden = json.loads(GOLDEN.read_text())
    assert set(produced["per_strategy"]) == set(golden["per_strategy"]) == {"momentum", "meanrev"}
    for sid in ("momentum", "meanrev"):
        assert produced["per_strategy"][sid] == golden["per_strategy"][sid]
    assert produced["combined"] == golden["combined"]


if __name__ == "__main__":  # regen helper: writes the committed golden
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        report = _produce(Path(tmp))
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN}")
