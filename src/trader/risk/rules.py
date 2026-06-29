"""Individual risk rules (design §10). Each is a pure function ``(order, ctx) ->
RuleResult`` evaluated on the RESULTING position and **fail-closed**: missing, stale,
or uncertain data rejects. The gate (M4.3) composes these into the single chokepoint.
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


def allowlist_denylist(order: Order, ctx: RuleContext) -> RuleResult:
    if order.symbol in ctx.config.denylist:
        return _reject(f"{order.symbol} is denylisted")
    if ctx.config.allowlist and order.symbol not in ctx.config.allowlist:
        return _reject(f"{order.symbol} not in allowlist")
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
    mid = (quote.bid + quote.ask) / 2
    if mid <= 0:
        return _reject("non-positive mid")
    spread_pct = (quote.ask - quote.bid) / mid * 100
    if spread_pct > Decimal(str(ctx.config.max_spread_pct)):
        return _reject(f"spread {spread_pct:.2f}% exceeds {ctx.config.max_spread_pct}%")
    age_s = (ctx.now - quote.ts).total_seconds()
    if age_s > ctx.config.max_staleness_seconds:
        return _reject(f"stale quote ({age_s:.0f}s > {ctx.config.max_staleness_seconds}s)")
    return RuleResult(ok=True)


def max_order_notional(order: Order, ctx: RuleContext) -> RuleResult:
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
    price = _ref_price(order, ctx)
    if price is None or price <= 0:
        return _reject("no price (fail closed)")
    gross = sum((abs(p.market_value) for p in ctx.positions), Decimal(0))
    new_notional = Decimal(order.quantity) * price
    if gross + new_notional > ctx.config.max_gross_exposure_usd:
        return _reject(
            f"gross exposure {gross + new_notional} exceeds cap {ctx.config.max_gross_exposure_usd}"
        )
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


# Ordered for the gate (M4.3): cheap gates first, then price-dependent caps.
ALL_RULES = (
    allowlist_denylist,
    duplicate_order_guard,
    daily_loss_limit,
    max_trades_per_day,
    price_sanity,
    max_order_notional,
    max_position_size,
    max_gross_exposure,
)
