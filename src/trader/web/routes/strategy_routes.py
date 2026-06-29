"""Per-strategy monitoring views (design §19.3, M7.8).

Lists each configured strategy (enabled / params / universe / trades-today vs limit) and a
detail page with the per-strategy attributed positions and the recent decision audit chain
(inputs -> decision -> risk verdict -> order -> fill, from the audit log). Read-only; the
limit comparisons are DISPLAY ONLY (no enforcement here). Unknown strategy id -> 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from trader.web.auth import require_session
from trader.web.views import base_context

router = APIRouter(dependencies=[Depends(require_session)])


def _strategies_data(request: Request) -> dict[str, Any]:
    repo = request.app.state.repo
    cfg = repo.config_view()
    risk = cfg.get("risk", {}) if isinstance(cfg.get("risk"), dict) else {}
    default_limit = risk.get("max_trades_per_day")
    today = request.app.state.now().date().isoformat()
    counts = repo.trades_today_by_strategy(today)
    strategies = []
    for sb in cfg.get("strategies", []) or []:
        sid = sb.get("id")
        overrides = sb.get("risk_overrides") or {}
        strategies.append(
            {
                "id": sid,
                "name": sb.get("name"),
                "enabled": bool(sb.get("enabled", True)),
                "universe": sb.get("universe", []),
                "params": sb.get("params", {}),
                "trades_today": counts.get(str(sid), 0),
                "trades_limit": overrides.get("max_trades_per_day", default_limit),
            }
        )
    return {"strategies": strategies, "updated_at": request.app.state.now().isoformat()}


def _strategy_ids(request: Request) -> set[str]:
    cfg = request.app.state.repo.config_view()
    ids = {str(sb.get("id")) for sb in (cfg.get("strategies") or []) if sb.get("id")}
    return ids | set(request.app.state.repo.strategy_list())


def _detail_data(request: Request, strategy_id: str) -> dict[str, Any]:
    repo = request.app.state.repo
    cfg = repo.config_view()
    binding: dict[str, Any] = next(
        (sb for sb in (cfg.get("strategies") or []) if str(sb.get("id")) == strategy_id), {}
    )
    detail = repo.strategy_detail(strategy_id)
    return {
        "strategy_id": strategy_id,
        "binding": binding,
        "attributed_positions": detail["attributed_positions"],
        "decisions": detail["recent_decisions"],
        "updated_at": request.app.state.now().isoformat(),
    }


def _render(request: Request, template: str, extra: dict[str, Any], user: str) -> Response:
    page: Response = request.app.state.templates.TemplateResponse(
        request, template, {**base_context(request, user), **extra}
    )
    return page


@router.get("/strategies")
def strategies(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "strategies.html", _strategies_data(request), user)


@router.get("/strategies/fragment")
def strategies_fragment(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "_partials/strategies_body.html", _strategies_data(request), user)


@router.get("/strategies/{strategy_id}")
def strategy_detail(
    request: Request, strategy_id: str, user: str = Depends(require_session)
) -> Response:
    if strategy_id not in _strategy_ids(request):
        raise HTTPException(status_code=404, detail="unknown strategy")
    return _render(request, "strategy_detail.html", _detail_data(request, strategy_id), user)


@router.get("/strategies/{strategy_id}/fragment")
def strategy_detail_fragment(
    request: Request, strategy_id: str, user: str = Depends(require_session)
) -> Response:
    if strategy_id not in _strategy_ids(request):
        raise HTTPException(status_code=404, detail="unknown strategy")
    return _render(
        request, "_partials/strategy_detail_body.html", _detail_data(request, strategy_id), user
    )


__all__ = ["router"]
