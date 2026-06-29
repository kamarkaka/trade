"""Orders / Fills monitoring views (design §19.3, M7.9).

Recent orders with status + fees, the risk-rejection tail (with reasons, from the audit log),
and an order detail page (current status + fills). Read-only; unknown order id -> 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from trader.web.auth import require_session
from trader.web.views import base_context

router = APIRouter(dependencies=[Depends(require_session)])


def _orders_data(request: Request) -> dict[str, Any]:
    repo = request.app.state.repo
    return {
        "orders": repo.recent_orders(limit=100),
        "rejections": repo.recent_rejections(limit=50),
        "updated_at": request.app.state.now().isoformat(),
    }


def _render(request: Request, template: str, extra: dict[str, Any], user: str) -> Response:
    page: Response = request.app.state.templates.TemplateResponse(
        request, template, {**base_context(request, user), **extra}
    )
    return page


@router.get("/orders")
def orders(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "orders.html", _orders_data(request), user)


@router.get("/orders/fragment")
def orders_fragment(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "_partials/orders_body.html", _orders_data(request), user)


@router.get("/orders/{order_id}")
def order_detail(request: Request, order_id: str, user: str = Depends(require_session)) -> Response:
    order = request.app.state.repo.order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="unknown order")
    extra = {
        "order": order,
        "fills": request.app.state.repo.order_fills(order_id),
        "updated_at": request.app.state.now().isoformat(),
    }
    return _render(request, "order_detail.html", extra, user)


__all__ = ["router"]
