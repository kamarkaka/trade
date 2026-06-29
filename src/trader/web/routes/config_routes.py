"""Read-only config view (design §19.3/§19.4, M7.9).

Renders the effective config (mode, schedule, strategies, risk, execution, alerting) from the
mounted config.yaml via the repo's ``config_view`` — with all secret-ish values scrubbed. The
page states that changes are made via the CLI / config file and executes nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from trader.web.auth import require_session
from trader.web.views import base_context

router = APIRouter(dependencies=[Depends(require_session)])


@router.get("/config")
def config(request: Request, user: str = Depends(require_session)) -> Response:
    extra: dict[str, Any] = {
        "config": request.app.state.repo.config_view(),  # already secret-scrubbed
        "updated_at": request.app.state.now().isoformat(),
    }
    page: Response = request.app.state.templates.TemplateResponse(
        request, "config.html", {**base_context(request, user), **extra}
    )
    return page


__all__ = ["router"]
