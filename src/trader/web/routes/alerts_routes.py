"""Alerts / log-tail monitoring view (design §19.3, M7.9).

Tails the alert-worthy audit events (kill-switch trips, cycle errors, risk rejections — see
``MonitoringRepo.recent_alerts``) most-recent-first. Read-only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from trader.web.auth import require_session
from trader.web.views import base_context

router = APIRouter(dependencies=[Depends(require_session)])


def _alerts_data(request: Request) -> dict[str, Any]:
    return {
        "alerts": request.app.state.repo.recent_alerts(limit=100),
        "updated_at": request.app.state.now().isoformat(),
    }


def _render(request: Request, template: str, extra: dict[str, Any], user: str) -> Response:
    page: Response = request.app.state.templates.TemplateResponse(
        request, template, {**base_context(request, user), **extra}
    )
    return page


@router.get("/alerts")
def alerts(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "alerts.html", _alerts_data(request), user)


@router.get("/alerts/fragment")
def alerts_fragment(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "_partials/alerts_body.html", _alerts_data(request), user)


__all__ = ["router"]
