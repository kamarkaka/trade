"""Sizing: Decision -> Order (design §4.2/§5).

Turns a strategy's share-delta ``Decision`` into a concrete ``Order`` with a
pre-generated ``client_order_id`` (the idempotency seed, generated BEFORE submit) and
``strategy_id`` attribution. Intentionally thin in M3: it does NOT clamp or limit —
that is the risk gate's job (M5). The uuid factory is injectable for deterministic
tests.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from trader.config.models import ExecutionConfig
from trader.core import Decision, Order
from trader.core.enums import Action, OrderType, Side, TimeInForce


def size_decision(
    decision: Decision,
    strategy_id: str,
    exec_cfg: ExecutionConfig,
    *,
    id_factory: Callable[[], str] = lambda: uuid4().hex,
) -> Order | None:
    """Build an Order from a Decision, or None for HOLD / non-positive quantity."""
    if decision.action is Action.HOLD or decision.quantity <= 0:
        return None
    side = Side.BUY if decision.action is Action.BUY else Side.SELL
    order_type = exec_cfg.order_type
    # limit_price only travels on LIMIT orders (a MARKET Order must not carry one).
    # A LIMIT exec config with a decision that supplied no limit_price is a strategy
    # bug: Order() will raise (fail loud) rather than silently drop the intent.
    limit_price = decision.limit_price if order_type is OrderType.LIMIT else None
    return Order(
        client_order_id=id_factory(),
        strategy_id=strategy_id,
        symbol=decision.symbol,
        side=side,
        quantity=decision.quantity,
        order_type=order_type,
        limit_price=limit_price,
        tif=TimeInForce.DAY,
    )
