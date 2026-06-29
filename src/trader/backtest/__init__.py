"""Backtest engine package: portfolio/P&L (M2.7), the event-driven engine (M2.8),
the run manifest (M2.9), and reporting (M2.10)."""

from .engine import BacktestEngine, BacktestResult, MultiStrategyResult, run_multi_strategy
from .manifest import build_manifest, config_hash, write_manifest
from .portfolio import Portfolio
from .report import (
    BacktestReportDoc,
    BacktestRunResult,
    FireRecord,
    build_multi_report,
    build_report,
)
from .rng import make_rng

__all__ = [
    "BacktestEngine",
    "BacktestReportDoc",
    "BacktestResult",
    "BacktestRunResult",
    "FireRecord",
    "MultiStrategyResult",
    "Portfolio",
    "build_manifest",
    "build_multi_report",
    "build_report",
    "config_hash",
    "make_rng",
    "run_multi_strategy",
    "write_manifest",
]
