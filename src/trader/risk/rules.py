"""Individual risk rules (design §10). Each is a pure function ``(order, ctx) ->
RuleResult`` evaluated on the RESULTING position and **fail-closed**: missing, stale,
or uncertain data rejects. The gate (M4.3) composes these into the single chokepoint.

Two invariants from §10 shape these rules:

1. **Evaluate the resulting position, not the order in isolation.** Caps look at where
   the book ends up after the order fills.
2. **Never block de-risking.** An order that does not *increase* a symbol's absolute
   exposure (a reduce/flatten/partial-exit) can never breach a sizing cap, so the
   notional / position-size / gross-exposure rules exempt it. (Bad-data and policy gates
   such as ``price_sanity`` / ``allowlist_denylist`` still apply to every order — with
   auto-flatten OFF by default we deliberately do not force trades on uncertain data.)

The per-strategy vs account-wide limit-scope merge is owned by the gate (M4.3), not by
these primitives. The kill-switch check (``DayState.kill_switch_engaged``) is enforced
separately at cycle start and pre-submit in M5.4.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import Account, DayState, Order, Position, Quote
from trader.core.enums import OrderType, Side


@dataclass(frozen=True)
class RuleResult:
    """A rule's outcome: pass, pass-with-clamp (clamped_quantity), or reject (ok=False)."""

    ok: bool
    reason: str = ""
    clamped_quantity: int | None = None


@dataclass(frozen=True)
class RuleContext:
    config: RiskConfig
    positions: Sequence[Position]
    account: Account
    quote: Quote | None  # the order symbol's quote; None => missing data => fail closed
    day_state: DayState
    now: datetime
    seen_client_order_ids: frozenset[str] = frozenset()


def _reject(reason: str) -> RuleResult:
    return RuleResult(ok=False, reason=reason)


def _ref_price(order: Order, ctx: RuleContext) -> Decimal | None:
    if order.order_type is OrderType.LIMIT and order.limit_price is not None:
        return order.limit_price
    return ctx.quote.last if ctx.quote is not None else None


def _position_qty(symbol: str, positions: Sequence[Position]) -> int:
    return next((p.quantity for p in positions if p.symbol == symbol), 0)


def _signed(order: Order) -> int:
    return order.quantity if order.side is Side.BUY else -order.quantity


def _reduces_or_holds_exposure(order: Order, ctx: RuleContext) -> bool:
    """True when the order does not increase the symbol's absolute position (a
    de-risking / flattening order). Such an order cannot raise notional, position
    size, or gross exposure, so the sizing caps must let it through — we must never
    prevent cutting risk, even when already over a cap (design §10)."""
    current = _position_qty(order.symbol, ctx.positions)
    return abs(current + _signed(order)) <= abs(current)


def kill_switch(order: Order, ctx: RuleContext) -> RuleResult:
    """Hard emergency stop: when the kill switch is engaged, halt ALL new orders (including
    de-risking exits -- auto-flatten is off, so exit manually if needed). Design §10."""
    if ctx.day_state.kill_switch_engaged:
        return _reject("kill switch engaged; all new orders halted")
    return RuleResult(ok=True)


def allowlist_denylist(order: Order, ctx: RuleContext) -> RuleResult:
    symbol = order.symbol.strip().upper()
    if symbol in ctx.config.denylist:
        return _reject(f"{symbol} is denylisted")
    if ctx.config.allowlist and symbol not in ctx.config.allowlist:
        return _reject(f"{symbol} not in allowlist")
    return RuleResult(ok=True)


def duplicate_order_guard(order: Order, ctx: RuleContext) -> RuleResult:
    if order.client_order_id in ctx.seen_client_order_ids:
        return _reject(f"duplicate client_order_id {order.client_order_id}")
    return RuleResult(ok=True)


def price_sanity(order: Order, ctx: RuleContext) -> RuleResult:
    quote = ctx.quote
    if quote is None:
        return _reject("no quote (fail closed)")
    if quote.last <= 0:
        return _reject(f"non-positive price {quote.last}")
    if quote.bid > quote.ask:
        return _reject(f"crossed market (bid {quote.bid} > ask {quote.ask})")
    mid = (quote.bid + quote.ask) / 2
    if mid <= 0:
        return _reject("non-positive mid")
    spread_pct = (quote.ask - quote.bid) / mid * 100
    if spread_pct > Decimal(str(ctx.config.max_spread_pct)):
        return _reject(f"spread {spread_pct:.2f}% exceeds {ctx.config.max_spread_pct}%")
    age_s = (ctx.now - quote.ts).total_seconds()
    if age_s > ctx.config.max_staleness_seconds:
        return _reject(f"stale quote ({age_s:.0f}s > {ctx.config.max_staleness_seconds}s)")
    band = ctx.config.max_deviation_from_prev_close_pct
    if band > 0 and quote.prev_close is not None and quote.prev_close > 0:
        deviation = abs(quote.last - quote.prev_close) / quote.prev_close * 100
        if deviation > Decimal(str(band)):
            return _reject(
                f"price {quote.last} deviates {deviation:.1f}% from prev close "
                f"{quote.prev_close} (> {band}% band; bad tick)"
            )
    return RuleResult(ok=True)


def max_order_notional(order: Order, ctx: RuleContext) -> RuleResult:
    if _reduces_or_holds_exposure(order, ctx):
        return RuleResult(ok=True)  # exits/reductions are not subject to the entry cap
    if ctx.quote is None:
        return _reject("no quote (fail closed)")
    price = _ref_price(order, ctx)
    if price is None or price <= 0:
        return _reject("no price (fail closed)")
    cap = ctx.config.max_order_notional_usd
    if Decimal(order.quantity) * price <= cap:
        return RuleResult(ok=True)
    allowed = int(cap / price)
    if allowed <= 0:
        return _reject(f"order notional exceeds cap {cap} and zero shares fit")
    return RuleResult(ok=True, reason=f"clamped to notional cap {cap}", clamped_quantity=allowed)


def max_position_size(order: Order, ctx: RuleContext) -> RuleResult:
    if _reduces_or_holds_exposure(order, ctx):
        return RuleResult(ok=True)  # reducing the position can never breach its cap
    if ctx.quote is None:
        return _reject("no quote (fail closed)")
    price = _ref_price(order, ctx)
    if price is None or price <= 0:
        return _reject("no price (fail closed)")
    resulting = abs(_position_qty(order.symbol, ctx.positions) + _signed(order))
    max_value = ctx.account.equity * Decimal(str(ctx.config.max_position_size_pct)) / Decimal(100)
    max_shares = int(max_value / price)
    if resulting > max_shares:
        return _reject(
            f"resulting position {resulting} exceeds cap {max_shares} "
            f"({ctx.config.max_position_size_pct}% of equity)"
        )
    return RuleResult(ok=True)


def max_gross_exposure(order: Order, ctx: RuleContext) -> RuleResult:
    if _reduces_or_holds_exposure(order, ctx):
        return RuleResult(ok=True)  # a reduction lowers (never raises) gross exposure
    if ctx.quote is None:
        return _reject("no quote (fail closed)")
    price = _ref_price(order, ctx)
    if price is None or price <= 0:
        return _reject("no price (fail closed)")
    # Gross on the RESULTING book: keep every other symbol at its mark, revalue the
    # order's symbol at its resulting size. (Adding full new notional on top of the
    # existing same-symbol market value would double-count and is wrong for add-ons.)
    resulting_qty = _position_qty(order.symbol, ctx.positions) + _signed(order)
    gross = sum(
        (abs(p.market_value) for p in ctx.positions if p.symbol != order.symbol), Decimal(0)
    )
    gross += abs(Decimal(resulting_qty) * price)
    cap = ctx.config.max_gross_exposure_usd
    if gross > cap:
        return _reject(f"gross exposure {gross} exceeds cap {cap}")
    return RuleResult(ok=True)


def daily_loss_limit(order: Order, ctx: RuleContext) -> RuleResult:
    limit = (
        ctx.day_state.start_of_day_equity
        * Decimal(str(ctx.config.daily_loss_limit_pct))
        / Decimal(100)
    )
    if ctx.day_state.loss_today >= limit:
        return _reject(
            f"daily loss {ctx.day_state.loss_today} hit limit {limit}; halting new orders"
        )
    return RuleResult(ok=True)


def max_trades_per_day(order: Order, ctx: RuleContext) -> RuleResult:
    if ctx.day_state.trades_today >= ctx.config.max_trades_per_day:
        return _reject(
            f"trades today {ctx.day_state.trades_today} hit limit {ctx.config.max_trades_per_day}"
        )
    return RuleResult(ok=True)


# Ordered for the gate (M4.3): the kill switch first (hardest stop), then cheap gates, then
# price-dependent caps.
ALL_RULES = (
    kill_switch,
    allowlist_denylist,
    duplicate_order_guard,
    daily_loss_limit,
    max_trades_per_day,
    price_sanity,
    max_order_notional,
    max_position_size,
    max_gross_exposure,
)
