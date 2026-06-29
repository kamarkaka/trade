"""Jinja2 environment for the monitoring UI (design §19, M7.6).

``make_templates()`` builds the shared ``Jinja2Templates`` with the filters every view uses:
``nyt`` (render an ISO-8601 UTC timestamp in America/New_York) and ``badge`` (map a semantic
level to a CSS class). Centralized so the app factory and the template tests configure the
environment identically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_NY = ZoneInfo("America/New_York")

_BADGE_CLASSES = {
    "ok": "badge badge-ok",
    "warn": "badge badge-warn",
    "alert": "badge badge-alert",
}


def nyt(value: object) -> str:
    """Format an ISO-8601 timestamp (assumed UTC if naive) in America/New_York. Returns
    ``"—"`` for empty and the raw value unchanged if it isn't a parseable timestamp."""
    if value in (None, ""):
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_NY).strftime("%Y-%m-%d %H:%M:%S %Z")


def badge(level: object) -> str:
    """CSS class for a semantic level (``ok`` / ``warn`` / ``alert``)."""
    return _BADGE_CLASSES.get(str(level), "badge")


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["nyt"] = nyt
    templates.env.filters["badge"] = badge
    return templates


__all__ = ["badge", "make_templates", "nyt"]
