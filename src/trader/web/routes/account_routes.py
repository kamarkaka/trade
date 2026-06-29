"""Account / P&L monitoring view (design §19.3, M7.8).

Broker-truth positions, cash / equity / daily P&L, gross exposure vs the configured limit,
and daily-loss vs the configured limit — each with an ok/amber/red proximity badge. Read-only:
limits come from ``config_view`` and the comparisons are DISPLAY ONLY (no enforcement). Money
is summed as ``Decimal`` from the stored strings (no float surprises).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from trader.web.auth import require_session
from trader.web.views import base_context

router = APIRouter(dependencies=[Depends(require_session)])


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _proximity(used: Decimal, limit: Decimal) -> str:
    """ok / warn / alert as ``used`` approaches ``limit`` (>=100% alert, >=80% warn)."""
    if limit <= 0:
        return "ok"
    ratio = used / limit
    if ratio >= 1:
        return "alert"
    if ratio >= Decimal("0.8"):
        return "warn"
    return "ok"


def _account_data(request: Request) -> dict[str, Any]:
    repo = request.app.state.repo
    cfg = repo.config_view()
    risk = cfg.get("risk", {}) if isinstance(cfg.get("risk"), dict) else {}
    positions = repo.positions_account()
    summary = repo.account_summary()
    today = summary.get("today") or {}

    gross = sum((abs(_dec(p.get("market_value"))) for p in positions), Decimal("0"))
    gross_limit = _dec(risk.get("max_gross_exposure_usd"))

    # Daily loss vs limit = start_of_day_equity * daily_loss_limit_pct%.
    sod_equity = _dec(today.get("start_of_day_equity"))
    loss_pct = _dec(risk.get("daily_loss_limit_pct"))
    loss_limit = (sod_equity * loss_pct / Decimal("100")) if sod_equity > 0 else Decimal("0")
    loss_today = _dec(today.get("loss_today"))

    return {
        "positions": positions,
        "latest_equity": summary.get("latest_equity"),
        "today": today,
        "gross_exposure": str(gross),
        "gross_limit": str(gross_limit) if gross_limit > 0 else None,
        "gross_badge": _proximity(gross, gross_limit),
        "loss_today": str(loss_today),
        "loss_limit": str(loss_limit) if loss_limit > 0 else None,
        "loss_badge": _proximity(loss_today, loss_limit),
        "updated_at": request.app.state.now().isoformat(),
    }


def _render(request: Request, template: str, extra: dict[str, Any], user: str) -> Response:
    page: Response = request.app.state.templates.TemplateResponse(
        request, template, {**base_context(request, user), **extra}
    )
    return page


@router.get("/account")
def account(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "account.html", _account_data(request), user)


@router.get("/account/fragment")
def account_fragment(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "_partials/account_body.html", _account_data(request), user)


__all__ = ["router"]
