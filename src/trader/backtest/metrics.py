"""Backtest performance metrics (design §9, M6.5).

Pure, deterministic analytics over a run's recorded equity curve and trades. ONE
calculation layer drives both reporting (M6.6/M6.7) and per-strategy vs combined
comparison: every function takes an optional ``strategy_id`` filter so the SAME code
produces a single strategy's numbers and the whole book's numbers.

Everything is ``Decimal`` and computed under a pinned context (prec 28, ROUND_HALF_EVEN)
so results don't depend on the caller's ambient decimal context — the golden-run test
(M6.8) relies on this. No plotting, no I/O, no wall-clock.

Inputs are the structures M2/M3 already emit:
  * equity curve  -> ``Sequence[tuple[datetime, Decimal]]`` (Portfolio.equity_series /
    MultiStrategyResult.equity_curve)
  * trades        -> ``Sequence[TradeRecord]`` adapted from ``(Fill, Side)`` pairs
    (see ``trade_records_from_multi``); each carries a ``strategy_id`` for filtering.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from trader.core import Fill
from trader.core.enums import Side

# Pinned context so metrics are independent of the caller's ambient decimal context
# (mirrors strategy.indicators._CTX — load-bearing for golden-run reproducibility).
_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
_ZERO = Decimal("0")
SESSIONS_PER_YEAR = 252


@dataclass(frozen=True)
class TradeRecord:
    """One execution with the side + owning strategy attached (a ``Fill`` carries no
    side, and metrics need both side and ``strategy_id`` for FIFO round-trips/filtering)."""

    ts: datetime
    strategy_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    fees: Decimal


@dataclass(frozen=True)
class Metrics:
    """Summary statistics for one strategy or the combined book."""

    start_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    cagr: Decimal
    max_drawdown_pct: Decimal
    max_dd_window: tuple[datetime | None, datetime | None]
    hit_rate: Decimal | None
    num_trades: int
    turnover: Decimal
    avg_exposure: Decimal | None


# --------------------------------------------------------------------------- #
# Adapters                                                                     #
# --------------------------------------------------------------------------- #


def trade_records_from_multi(
    per_strategy_trades: dict[str, list[tuple[Fill, Side]]],
) -> list[TradeRecord]:
    """Flatten ``MultiStrategyResult.per_strategy_trades`` into ``TradeRecord``s
    (chronological, tagged with their ``strategy_id``)."""
    records = [
        TradeRecord(
            ts=fill.ts,
            strategy_id=strategy_id,
            symbol=fill.symbol,
            side=side,
            quantity=fill.quantity,
            price=fill.price,
            fees=fill.fees,
        )
        for strategy_id, trades in per_strategy_trades.items()
        for fill, side in trades
        if fill.quantity > 0
    ]
    records.sort(key=lambda t: t.ts)
    return records


def _select(trades: Iterable[TradeRecord], strategy_id: str | None) -> list[TradeRecord]:
    return [t for t in trades if strategy_id is None or t.strategy_id == strategy_id]


# --------------------------------------------------------------------------- #
# Equity-curve metrics                                                         #
# --------------------------------------------------------------------------- #


def build_equity_curve(
    points: Iterable[tuple[datetime, Decimal]],
) -> list[tuple[datetime, Decimal]]:
    """Chronologically sorted equity points (stable on equal timestamps)."""
    return sorted(points, key=lambda p: p[0])


def total_return(curve: Sequence[tuple[datetime, Decimal]]) -> Decimal:
    """``(final - start) / start``. ``0`` for an empty/single-point curve or start<=0."""
    if len(curve) < 2:
        return _ZERO
    start, final = curve[0][1], curve[-1][1]
    if start <= 0:
        return _ZERO
    with localcontext(_CTX):
        return (final - start) / start


def cagr(
    curve: Sequence[tuple[datetime, Decimal]],
    *,
    sessions_per_year: int = SESSIONS_PER_YEAR,
) -> Decimal:
    """Annualized compound growth, using ``len(curve)`` as the session count
    (``years = sessions / sessions_per_year``). ``0`` if the curve is too short or
    either endpoint is non-positive (can't take a ratio's log)."""
    sessions = len(curve)
    if sessions < 2:
        return _ZERO
    start, final = curve[0][1], curve[-1][1]
    if start <= 0 or final <= 0:
        return _ZERO
    with localcontext(_CTX):
        years = Decimal(sessions) / Decimal(sessions_per_year)
        ratio = final / start
        # ratio ** (1/years) via exp/ln (Decimal supports both deterministically here).
        return (ratio.ln() / years).exp() - 1


def max_drawdown(
    curve: Sequence[tuple[datetime, Decimal]],
) -> tuple[datetime | None, datetime | None, Decimal]:
    """Largest peak-to-trough decline as a positive fraction, plus the peak/trough
    timestamps, in a single forward pass tracking the running peak. ``(None, None, 0)``
    for an empty/single-point curve or a curve that only rises."""
    if len(curve) < 2:
        return (None, None, _ZERO)
    peak_ts, peak_val = curve[0]
    worst_dd = _ZERO
    worst_peak_ts: datetime | None = None
    worst_trough_ts: datetime | None = None
    with localcontext(_CTX):
        for ts, val in curve:
            if val > peak_val:
                peak_val, peak_ts = val, ts
            elif peak_val > 0:
                dd = (peak_val - val) / peak_val
                if dd > worst_dd:
                    worst_dd, worst_peak_ts, worst_trough_ts = dd, peak_ts, ts
    return (worst_peak_ts, worst_trough_ts, worst_dd)


def avg_equity(curve: Sequence[tuple[datetime, Decimal]]) -> Decimal:
    """Mean equity across the curve (``0`` for an empty curve)."""
    if not curve:
        return _ZERO
    with localcontext(_CTX):
        return sum((v for _, v in curve), _ZERO) / Decimal(len(curve))


# --------------------------------------------------------------------------- #
# Trade metrics                                                                #
# --------------------------------------------------------------------------- #


def hit_rate(
    trades: Sequence[TradeRecord],
    *,
    strategy_id: str | None = None,
) -> Decimal | None:
    """Fraction of closing trades that realized a positive price P&L, pairing
    entries/exits FIFO per ``(strategy_id, symbol)``. A "round trip" is one closing
    trade (the trade that reduces/flips an existing position); its P&L is summed across
    every open lot it consumes. Open positions never closed are excluded. Returns
    ``None`` when there are no closing trades (nothing to score)."""
    selected = _select(trades, strategy_id)
    # FIFO of open lots per key: deque of [signed_remaining_qty, price].
    open_lots: dict[tuple[str, str], deque[list[Decimal]]] = {}
    wins = 0
    closed = 0
    with localcontext(_CTX):
        for t in sorted(selected, key=lambda x: x.ts):
            key = (t.strategy_id, t.symbol)
            lots = open_lots.setdefault(key, deque())
            signed = Decimal(t.quantity) if t.side is Side.BUY else -Decimal(t.quantity)
            price = t.price
            # Consume against opposite-sign open lots first (this is a closing trade).
            realized = _ZERO
            matched = False
            while lots and signed != 0 and (lots[0][0] > 0) != (signed > 0):
                matched = True
                lot = lots[0]
                lot_qty, lot_price = lot[0], lot[1]
                take = min(abs(lot_qty), abs(signed))
                if lot_qty > 0:  # closing a long: profit when sell price > cost
                    realized += (price - lot_price) * take
                else:  # closing a short: profit when buy price < proceeds
                    realized += (lot_price - price) * take
                lot[0] = lot_qty - take if lot_qty > 0 else lot_qty + take
                signed = signed + take if signed < 0 else signed - take
                if lot[0] == 0:
                    lots.popleft()
            if matched:
                closed += 1
                if realized > 0:
                    wins += 1
            # Any remainder opens (or extends) a position in the trade's direction.
            if signed != 0:
                lots.append([signed, price])
    if closed == 0:
        return None
    with localcontext(_CTX):
        return Decimal(wins) / Decimal(closed)


def turnover(
    trades: Sequence[TradeRecord],
    avg_equity_value: Decimal,
    *,
    strategy_id: str | None = None,
) -> Decimal:
    """Total traded notional divided by average equity. ``0`` if avg equity <= 0."""
    if avg_equity_value <= 0:
        return _ZERO
    with localcontext(_CTX):
        notional = sum(
            (Decimal(t.quantity) * t.price for t in _select(trades, strategy_id)),
            _ZERO,
        )
        return notional / avg_equity_value


def avg_exposure(exposure_series: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Mean of ``gross_exposure / equity`` over a series of ``(gross_exposure, equity)``
    snapshots (points with equity <= 0 are skipped). ``None`` if there is no usable
    point."""
    with localcontext(_CTX):
        ratios = [gross / eq for gross, eq in exposure_series if eq > 0]
        if not ratios:
            return None
        return sum(ratios, _ZERO) / Decimal(len(ratios))


# --------------------------------------------------------------------------- #
# Summary                                                                      #
# --------------------------------------------------------------------------- #


def summarize(
    curve: Sequence[tuple[datetime, Decimal]],
    trades: Sequence[TradeRecord],
    *,
    strategy_id: str | None = None,
    exposure_series: Sequence[tuple[Decimal, Decimal]] | None = None,
    sessions_per_year: int = SESSIONS_PER_YEAR,
) -> Metrics:
    """Bundle every metric for one strategy (``strategy_id``) or the combined book
    (``strategy_id=None``). ``curve`` is the equity curve to score; for per-strategy
    reporting the caller passes that strategy's equity contribution."""
    ordered = build_equity_curve(curve)
    selected = _select(trades, strategy_id)
    peak_ts, trough_ts, dd = max_drawdown(ordered)
    start_equity = ordered[0][1] if ordered else _ZERO
    final_equity = ordered[-1][1] if ordered else _ZERO
    return Metrics(
        start_equity=start_equity,
        final_equity=final_equity,
        total_return=total_return(ordered),
        cagr=cagr(ordered, sessions_per_year=sessions_per_year),
        max_drawdown_pct=dd,
        max_dd_window=(peak_ts, trough_ts),
        hit_rate=hit_rate(selected),
        num_trades=len(selected),
        turnover=turnover(selected, avg_equity(ordered)),
        avg_exposure=avg_exposure(exposure_series) if exposure_series is not None else None,
    )


__all__ = [
    "Metrics",
    "TradeRecord",
    "avg_equity",
    "avg_exposure",
    "build_equity_curve",
    "cagr",
    "hit_rate",
    "max_drawdown",
    "summarize",
    "total_return",
    "trade_records_from_multi",
    "turnover",
]
