"""Backtest engine package: portfolio/P&L (M2.7), the event-driven engine (M2.8),
the run manifest (M2.9), and reporting (M2.10)."""

from .engine import BacktestEngine, BacktestResult
from .manifest import build_manifest, config_hash, write_manifest
from .portfolio import Portfolio
from .rng import make_rng

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "Portfolio",
    "build_manifest",
    "config_hash",
    "make_rng",
    "write_manifest",
]
