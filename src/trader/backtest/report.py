"""Backtest report (design §9.6).

``BacktestReport.build`` turns a run's fills + equity curve + manifest into a single
JSON-serializable report: a summary (P&L, max drawdown, hit rate, turnover), the
equity curve, and the trade blotter. Money is emitted as strings to preserve Decimal
precision. ``strip_volatile`` removes environment-sensitive manifest fields so a
golden run can be compared bit-for-bit across machines (M2.10).

M6.6 adds the reproducible per-strategy + combined report (``build_report`` ->
``BacktestReportDoc``) driven by the M6.5 metrics layer, rendering deterministic JSON
(feeds the M6.8 golden) and a Jinja2 HTML view. ``jinja2`` is imported lazily inside
the HTML renderer so the live container (which never renders reports) needn't ship it.

This file is CREATED in M2.10; M3.10 (per-strategy attribution) and M6.6 (HTML + richer
metrics) UPDATE it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, localcontext
from pathlib import Path
from typing import Any

from trader.core import Fill
from trader.core.enums import Side

from . import metrics as M
from .metrics import Metrics
from .portfolio import Portfolio

# Manifest fields that vary by environment and must be dropped before a golden compare.
VOLATILE_MANIFEST_FIELDS = ("git_commit", "lib_versions", "python_version")

# Ratio metrics are quantized to a fixed scale so the report is independent of the
# global decimal context (a non-terminating division would otherwise bake the
# context precision into the golden).
_RATIO_SCALE = Decimal("0.00000001")  # 8 dp

# Pinned context for ALL report arithmetic, so output is immune to the caller's ambient
# decimal context — a low-precision ambient context would otherwise make ``quantize``
# raise InvalidOperation (or a division round wrong) and break the golden's byte
# reproducibility. prec 28 matches strategy.indicators / metrics so values are identical.
_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

EquityPoint = tuple[datetime, Decimal]


def _q(value: Decimal) -> Decimal:
    with localcontext(_CTX):
        return value.quantize(_RATIO_SCALE, rounding=ROUND_HALF_UP)


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(_CTX):
        return numerator / denominator if denominator != 0 else Decimal("0")


def _max_drawdown(curve: Sequence[EquityPoint]) -> Decimal:
    if not curve:
        return Decimal("0")
    peak = curve[0][1]  # track the true running max from the start (handles all-negative)
    worst = Decimal("0")
    with localcontext(_CTX):
        for _, equity in curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
    return _q(worst)


def _hit_rate(curve: Sequence[EquityPoint]) -> Decimal:
    # Fraction of equity-curve *intervals* that rose (a curve proxy, NOT a per-trade
    # win rate; per-trade attribution arrives with M3).
    if len(curve) < 2:
        return Decimal("0")
    ups = sum(1 for i in range(1, len(curve)) if curve[i][1] > curve[i - 1][1])
    with localcontext(_CTX):
        return _q(Decimal(ups) / Decimal(len(curve) - 1))


def _turnover(fills: Sequence[Fill], starting_equity: Decimal) -> Decimal:
    with localcontext(_CTX):
        notional = sum((Decimal(f.quantity) * f.price for f in fills), Decimal("0"))
    return _q(_safe_div(notional, starting_equity))


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
                "total_return": str(_q(_safe_div(ending - starting, starting))),
                "max_drawdown": str(_max_drawdown(equity_curve)),
                "hit_rate": str(_hit_rate(equity_curve)),
                "turnover": str(_turnover(fills, starting)),
                "total_fees": str(sum((f.fees for f in fills), Decimal("0"))),
            },
            "equity_curve": [{"ts": ts.isoformat(), "equity": str(eq)} for ts, eq in equity_curve],
            "blotter": [_fill_row(f) for f in fills],
        }


def build_multi_report(
    per_strategy_trades: dict[str, list[tuple[Fill, Side]]],
    equity_curve: Sequence[EquityPoint],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Multi-strategy report: per-strategy blotter + realized P&L, plus the combined
    equity curve (design §9.6). Per-strategy realized P&L comes from a per-strategy
    book fed only that strategy's fills."""
    per_strategy: dict[str, Any] = {}
    for strategy_id in sorted(per_strategy_trades):
        trades = per_strategy_trades[strategy_id]
        book = Portfolio(Decimal("0"))  # zero-cash book: tracks realized P&L from fills
        for fill, side in trades:
            book.apply_fill(fill, side)
        per_strategy[strategy_id] = {
            "num_trades": len(trades),
            "realized_pnl": str(book.realized_pnl()),
            "total_fees": str(book.total_fees()),
            "blotter": [_fill_row(fill) for fill, _ in trades],
        }
    return {
        "manifest": manifest,
        "equity_curve": [{"ts": ts.isoformat(), "equity": str(eq)} for ts, eq in equity_curve],
        "per_strategy": per_strategy,
    }


def strip_volatile(report: dict[str, Any]) -> dict[str, Any]:
    """A copy with environment-sensitive manifest fields removed (golden compare)."""
    out = dict(report)
    manifest = out.get("manifest", {})
    out["manifest"] = {k: v for k, v in manifest.items() if k not in VOLATILE_MANIFEST_FIELDS}
    return out


# --------------------------------------------------------------------------- #
# M6.6 — per-strategy + combined report (HTML/JSON + manifest)                 #
# --------------------------------------------------------------------------- #

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class FireRecord:
    """One scheduler firing: the resolved trigger with its realized drift + seed."""

    strategy_id: str
    slot_id: str
    fire_ts: datetime
    drift_seconds: int
    seed: int | None = None


@dataclass(frozen=True)
class BacktestRunResult:
    """The inputs a report is built from (assembled by the M6.7 backtest CLI).

    ``combined_equity_curve`` is the ACCOUNT-level equity series — combined performance
    is measured against this, NOT the sum of per-strategy curves, because cash is shared
    across strategies (design §9.6). ``per_strategy_equity`` is an OPTIONAL per-strategy
    equity contribution; when omitted, a strategy's equity-curve metrics (return / CAGR /
    drawdown) are reported as ``null`` while its trade metrics (hit rate, turnover,
    realized P&L) are always computed. ``fire_log`` records realized drift + seed per
    firing for the per-slot section.
    """

    combined_equity_curve: Sequence[EquityPoint]
    per_strategy_trades: dict[str, list[tuple[Fill, Side]]]
    fire_log: Sequence[FireRecord] = field(default_factory=tuple)
    per_strategy_equity: dict[str, Sequence[EquityPoint]] | None = None


def _ratio_str(value: Decimal) -> str:
    # Fixed-point 8dp string. ``format(..., "f")`` (not ``str``) so an exact zero renders
    # "0.00000000" rather than "0E-8" — deterministic + readable in the golden JSON.
    return format(_q(value), "f")


def _opt_ratio(value: Decimal | None) -> str | None:
    return _ratio_str(value) if value is not None else None


def _window(window: tuple[datetime | None, datetime | None]) -> list[str | None]:
    peak, trough = window
    return [peak.isoformat() if peak else None, trough.isoformat() if trough else None]


def _metrics_dict(m: Metrics) -> dict[str, Any]:
    """Serialize a ``Metrics`` deterministically: ratios quantized to 8dp, money exact,
    timestamps ISO-8601 UTC."""
    return {
        "start_equity": str(m.start_equity),
        "final_equity": str(m.final_equity),
        "total_return": _ratio_str(m.total_return),
        "cagr": _ratio_str(m.cagr),
        "max_drawdown_pct": _ratio_str(m.max_drawdown_pct),
        "max_dd_window": _window(m.max_dd_window),
        "hit_rate": _opt_ratio(m.hit_rate),
        "num_trades": m.num_trades,
        "turnover": _ratio_str(m.turnover),
        "avg_exposure": _opt_ratio(m.avg_exposure),
    }


def _fire_row(fire: FireRecord) -> dict[str, Any]:
    return {
        "strategy_id": fire.strategy_id,
        "slot_id": fire.slot_id,
        "fire_ts": fire.fire_ts.isoformat(),
        "drift_seconds": fire.drift_seconds,
        "seed": fire.seed,
    }


def _sorted_fire(fire_log: Sequence[FireRecord]) -> list[FireRecord]:
    # Defensive deterministic order: the M6.7 engine should emit firings chronologically,
    # but sorting here keeps the report's JSON byte-stable regardless of how it assembles.
    return sorted(fire_log, key=lambda f: (f.fire_ts, f.strategy_id, f.slot_id))


def _equity_points(curve: Sequence[EquityPoint]) -> list[dict[str, str]]:
    return [{"ts": ts.isoformat(), "equity": str(eq)} for ts, eq in curve]


def _per_strategy_block(
    sid: str,
    trades: list[tuple[Fill, Side]],
    records: Sequence[M.TradeRecord],
    combined_avg_equity: Decimal,
    per_strategy_equity: Sequence[EquityPoint] | None,
    fire_log: Sequence[FireRecord],
) -> dict[str, Any]:
    # Realized P&L from a zero-cash book fed only this strategy's fills (the honest
    # per-strategy P&L, independent of the shared account curve). Run the book's Decimal
    # arithmetic under the pinned context so its output is ambient-context-independent
    # (avg-cost division would otherwise inherit the caller's precision -> golden drift).
    with localcontext(_CTX):
        book = Portfolio(Decimal("0"))
        for fill, side in trades:
            book.apply_fill(fill, side)
        realized_pnl, total_fees = book.realized_pnl(), book.total_fees()
    block: dict[str, Any] = {
        "num_trades": len(trades),
        "hit_rate": _opt_ratio(M.hit_rate(records, strategy_id=sid)),
        # Turnover uses the shared account's average equity as the denominator.
        "turnover": _ratio_str(M.turnover(records, combined_avg_equity, strategy_id=sid)),
        "realized_pnl": str(realized_pnl),
        "total_fees": str(total_fees),
        "blotter": [_fill_row(fill) for fill, _ in trades],
        "fire_log": [_fire_row(f) for f in _sorted_fire(fire_log) if f.strategy_id == sid],
    }
    if per_strategy_equity is not None:
        m = M.summarize(per_strategy_equity, records, strategy_id=sid)
        block["equity_metrics"] = _metrics_dict(m)
        block["equity_curve"] = _equity_points(per_strategy_equity)
    else:
        block["equity_metrics"] = None
        block["equity_curve"] = []
    return block


def build_report(
    run_result: BacktestRunResult,
    manifest: dict[str, Any],
    *,
    strategy_ids: Sequence[str] | None = None,
) -> BacktestReportDoc:
    """Assemble the per-strategy + combined report document (design §9.6).

    Combined metrics come from ``metrics.summarize`` over the ACCOUNT equity curve and
    every trade; each strategy gets its own section (one per enabled ``strategy_id``,
    including zero-trade strategies) computed by the SAME M6.5 layer.
    """
    curve = M.build_equity_curve(run_result.combined_equity_curve)
    records = M.trade_records_from_multi(run_result.per_strategy_trades)
    combined_avg_equity = M.avg_equity(curve)
    # Carry the owning strategy_id so the combined blotter has a deterministic tie-break
    # (sorting on ts alone would otherwise leak the per_strategy_trades dict order when
    # two fills share a timestamp — breaking the golden's byte reproducibility).
    all_fills = [
        (fill, sid) for sid, trades in run_result.per_strategy_trades.items() for fill, _ in trades
    ]
    ordered_fills = sorted(
        all_fills, key=lambda t: (t[0].ts, t[1], t[0].symbol, t[0].client_order_id)
    )

    combined = _metrics_dict(M.summarize(curve, records))
    with localcontext(_CTX):
        combined["total_fees"] = str(sum((f.fees for f, _ in all_fills), Decimal("0")))
    combined["blotter"] = [_fill_row(f) for f, _ in ordered_fills]
    combined["equity_curve"] = _equity_points(curve)
    combined["fire_log"] = [_fire_row(f) for f in _sorted_fire(run_result.fire_log)]

    # One section per enabled strategy (zero-trade strategies still get a section).
    sids = (
        list(strategy_ids) if strategy_ids is not None else sorted(run_result.per_strategy_trades)
    )
    per_strategy_equity = run_result.per_strategy_equity or {}
    per_strategy = {
        sid: _per_strategy_block(
            sid,
            run_result.per_strategy_trades.get(sid, []),
            records,
            combined_avg_equity,
            per_strategy_equity.get(sid),
            run_result.fire_log,
        )
        for sid in sids
    }
    data: dict[str, Any] = {
        "manifest": manifest,
        "combined": combined,
        "per_strategy": per_strategy,
    }
    return BacktestReportDoc(data)


@dataclass(frozen=True)
class BacktestReportDoc:
    """A built report: deterministic JSON + Jinja2 HTML renderings."""

    data: dict[str, Any]

    def json_str(self) -> str:
        """Deterministic JSON: sorted keys, compact-but-readable, trailing newline. Two
        runs of the same config produce byte-identical output (feeds the M6.8 golden)."""
        import json

        return json.dumps(self.data, indent=2, sort_keys=True) + "\n"

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(self.json_str(), encoding="utf-8")

    def html_str(self) -> str:
        # Lazy import: the live container never renders HTML and so needn't ship jinja2.
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        template = env.get_template("report.html.j2")
        return template.render(**self.data)

    def to_html(self, path: str | Path) -> None:
        Path(path).write_text(self.html_str(), encoding="utf-8")
