"""System / Schedule / Re-auth monitoring views (design §19.3, M7.7).

The router carries a ROUTER-LEVEL ``require_session`` dependency (default-deny: every route
here is authenticated; only the public /login,/logout,/healthz,/static live outside it). Each
view server-renders its body (so it works without JS and is testable) inside a div that the
HTMX shim re-fetches from the ``/fragment`` endpoint for auto-refresh.

Read-only: data comes from ``MonitoringRepo``; the token countdown shows the weekly re-auth
deadline (value only, never the token); /reauth DISPLAYS the CLI runbook but executes nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from trader.web.auth import require_session
from trader.web.views import base_context

router = APIRouter(dependencies=[Depends(require_session)])

# Schwab refresh-token cap (§8.2) + how many days ahead to warn (amber). Web-local constants;
# the trader enforces the authoritative value.
_REFRESH_MAX_AGE_DAYS = 7.0
_REAUTH_LEAD_DAYS = 2.0

# Cached XNYS calendar (heavy to build); market-open is best-effort (None => unknown).
_calendar: Any = None


def _market_open(now: Any) -> bool | None:
    global _calendar
    try:
        if _calendar is None:
            from trader.scheduler.calendar import TradingCalendar

            _calendar = TradingCalendar()
        result: bool = _calendar.is_open(now)
        return result
    except Exception:
        return None


def _token_view(request: Request) -> dict[str, Any]:
    now = request.app.state.now()
    status = dict(request.app.state.repo.token_status(now))
    if not status.get("authenticated"):
        status["badge_level"] = "alert"
        return status
    days_until = _REFRESH_MAX_AGE_DAYS - float(status["refresh_token_age_days"])
    status["days_until_refresh_expiry"] = days_until
    if days_until <= 0:
        status["badge_level"] = "alert"
    elif days_until <= _REAUTH_LEAD_DAYS:
        status["badge_level"] = "warn"
    else:
        status["badge_level"] = "ok"
    return status


def _system_data(request: Request) -> dict[str, Any]:
    now = request.app.state.now()
    repo = request.app.state.repo
    status = repo.system_status()
    heartbeat = status.get("heartbeat")
    cfg = repo.config_view()
    try:
        heartbeat_minutes = float(cfg.get("alerting", {}).get("heartbeat_minutes", 60) or 60)
    except (TypeError, ValueError):
        heartbeat_minutes = 60.0  # degrade gracefully on an odd config value (never 500)
    threshold_s = heartbeat_minutes * 60 * 2  # tolerate one missed beat
    stale = True
    age_minutes: float | None = None
    if heartbeat and heartbeat.get("last_alive_at"):
        from datetime import datetime

        try:
            last = datetime.fromisoformat(str(heartbeat["last_alive_at"]))
            if last.tzinfo is not None and now.tzinfo is not None:
                age = (now - last).total_seconds()
                age_minutes = age / 60.0
                stale = age > threshold_s
        except (TypeError, ValueError):
            stale = True
    return {
        "heartbeat": heartbeat,
        "heartbeat_stale": stale,
        "heartbeat_age_minutes": age_minutes,
        "market_open": _market_open(now),
        "token": _token_view(request),
        "updated_at": now.isoformat(),
    }


def _render(request: Request, template: str, extra: dict[str, Any], user: str) -> Response:
    ctx = {**base_context(request, user), **extra}
    page: Response = request.app.state.templates.TemplateResponse(request, template, ctx)
    return page


@router.get("/")
@router.get("/system")
def system(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "system.html", _system_data(request), user)


@router.get("/system/fragment")
def system_fragment(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "_partials/system_body.html", _system_data(request), user)


def _schedule_data(request: Request) -> dict[str, Any]:
    return {
        "slots": request.app.state.repo.schedule_status(limit=100),
        "updated_at": request.app.state.now().isoformat(),
    }


@router.get("/schedule")
def schedule(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "schedule.html", _schedule_data(request), user)


@router.get("/schedule/fragment")
def schedule_fragment(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "_partials/schedule_body.html", _schedule_data(request), user)


@router.get("/reauth")
def reauth(request: Request, user: str = Depends(require_session)) -> Response:
    return _render(request, "reauth.html", {"token": _token_view(request)}, user)


__all__ = ["router"]
