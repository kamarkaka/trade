"""Backtest report (design §9.6). JSON-first; HTML templating is added in M6.6.

``BacktestReport.build`` turns a run's fills + equity curve + manifest into a single
JSON-serializable report: a summary (P&L, max drawdown, hit rate, turnover), the
equity curve, and the trade blotter. Money is emitted as strings to preserve Decimal
precision. ``strip_volatile`` removes environment-sensitive manifest fields so a
golden run can be compared bit-for-bit across machines (M2.10).

This file is CREATED here; M3.10 (per-strategy attribution) and M6.6 (HTML + richer
metrics) UPDATE it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from trader.core import Fill

# Manifest fields that vary by environment and must be dropped before a golden compare.
VOLATILE_MANIFEST_FIELDS = ("git_commit", "lib_versions", "python_version")

EquityPoint = tuple[datetime, Decimal]


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator != 0 else Decimal("0")


def _max_drawdown(curve: Sequence[EquityPoint]) -> Decimal:
    peak = Decimal("0")
    worst = Decimal("0")
    for _, equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _hit_rate(curve: Sequence[EquityPoint]) -> Decimal:
    if len(curve) < 2:
        return Decimal("0")
    ups = sum(1 for i in range(1, len(curve)) if curve[i][1] > curve[i - 1][1])
    return Decimal(ups) / Decimal(len(curve) - 1)


def _turnover(fills: Sequence[Fill], starting_equity: Decimal) -> Decimal:
    notional = sum((Decimal(f.quantity) * f.price for f in fills), Decimal("0"))
    return _safe_div(notional, starting_equity)


def _fill_row(fill: Fill) -> dict[str, Any]:
    return {
        "ts": fill.ts.isoformat(),
        "symbol": fill.symbol,
        "quantity": fill.quantity,
        "price": str(fill.price),
        "fees": str(fill.fees),
        "status": fill.status.value,
        "client_order_id": fill.client_order_id,
    }


class BacktestReport:
    """Builds the JSON backtest report from run outputs."""

    @staticmethod
    def build(
        fills: Sequence[Fill],
        equity_curve: Sequence[EquityPoint],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        starting = equity_curve[0][1] if equity_curve else Decimal("0")
        ending = equity_curve[-1][1] if equity_curve else Decimal("0")
        return {
            "manifest": manifest,
            "summary": {
                "num_trades": len(fills),
                "starting_equity": str(starting),
                "ending_equity": str(ending),
                "total_return": str(_safe_div(ending - starting, starting)),
                "max_drawdown": str(_max_drawdown(equity_curve)),
                "hit_rate": str(_hit_rate(equity_curve)),
                "turnover": str(_turnover(fills, starting)),
                "total_fees": str(sum((f.fees for f in fills), Decimal("0"))),
            },
            "equity_curve": [{"ts": ts.isoformat(), "equity": str(eq)} for ts, eq in equity_curve],
            "blotter": [_fill_row(f) for f in fills],
        }


def strip_volatile(report: dict[str, Any]) -> dict[str, Any]:
    """A copy with environment-sensitive manifest fields removed (golden compare)."""
    out = dict(report)
    manifest = out.get("manifest", {})
    out["manifest"] = {k: v for k, v in manifest.items() if k not in VOLATILE_MANIFEST_FIELDS}
    return out
