"""Shared view helpers for the monitoring routers (M7.7+).

``base_context`` assembles the chrome context every page needs (logged-in user, trading mode,
kill-switch badge, a fresh CSRF token for the logout form, and the auto-refresh interval) so
each router stays thin. Read-only: it only reads via the repo + settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request

from trader.web.security import make_csrf_token

_DEFAULT_TZ = "America/New_York"


def exchange_session(cfg: Mapping[str, Any], now: datetime) -> tuple[date, datetime, datetime]:
    """(session date, start, end) of the exchange session containing ``now`` — in the
    configured ``schedule.timezone``, as the daemon's daily counters bucket orders."""
    schedule = cfg.get("schedule")
    name = schedule.get("timezone") if isinstance(schedule, Mapping) else None
    try:
        tz = ZoneInfo(str(name or _DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(_DEFAULT_TZ)
    session = now.astimezone(tz).date()
    start = datetime.combine(session, time(0), tzinfo=tz)
    return session, start, datetime.combine(session + timedelta(days=1), time(0), tzinfo=tz)


def base_context(request: Request, user: str) -> dict[str, Any]:
    settings = request.app.state.settings
    repo = request.app.state.repo
    kill = repo.system_status().get("kill_switch") or {}
    return {
        "user": user,
        "mode": repo.config_view().get("mode", "?"),
        "kill_switch": kill,
        "kill_switch_engaged": bool(kill.get("engaged")),
        "csrf_token": make_csrf_token(
            settings.session_secret.get_secret_value(), request.app.state.now()
        ),
        "refresh_seconds": settings.auto_refresh_seconds,
    }


__all__ = ["base_context", "exchange_session"]
