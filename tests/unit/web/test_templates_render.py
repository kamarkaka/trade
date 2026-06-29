"""Template chrome tests (M7.6): base layout, auto-refresh wrapper, local HTMX, filters."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from trader.web.app import create_app
from trader.web.settings import WebSettings
from trader.web.templating import badge, make_templates, nyt


def test_base_renders() -> None:
    tmpl = make_templates().env.get_template("base.html")
    out = tmpl.render(
        user="admin",
        mode="paper",
        kill_switch_engaged=False,
        csrf_token="csrf-abc",
    )
    # nav links present
    for href in ("/schedule", "/strategies", "/account", "/orders", "/alerts", "/config"):
        assert f'href="{href}"' in out
    # logout form carries the CSRF token
    assert 'action="/logout"' in out
    assert 'name="csrf" value="csrf-abc"' in out
    # local HTMX, no CDN
    assert "/static/htmx.min.js" in out
    assert "cdn" not in out.lower()
    assert "mode: paper" in out


def test_base_shows_kill_switch_badge() -> None:
    out = (
        make_templates()
        .env.get_template("base.html")
        .render(user="admin", mode="live", kill_switch_engaged=True, csrf_token="x")
    )
    assert "KILL SWITCH ENGAGED" in out


def test_refresh_wrapper_has_hx_attrs() -> None:
    out = (
        make_templates()
        .env.get_template("_partials/refresh_wrapper.html")
        .render(fragment_url="/system/fragment", refresh_seconds=15)
    )
    assert 'hx-get="/system/fragment"' in out
    assert 'hx-trigger="every 15s"' in out
    assert 'hx-swap="innerHTML"' in out


def test_nyt_filter() -> None:
    # UTC noon -> America/New_York (EDT, -4h in summer) = 08:00
    assert "08:00:00" in nyt("2026-06-29T12:00:00+00:00")
    assert nyt(None) == "—"
    assert nyt("") == "—"
    assert nyt("not-a-date") == "not-a-date"
    # naive assumed UTC
    assert "08:00:00" in nyt("2026-06-29T12:00:00")


def test_badge_filter() -> None:
    assert badge("ok") == "badge badge-ok"
    assert badge("warn") == "badge badge-warn"
    assert badge("alert") == "badge badge-alert"
    assert badge("other") == "badge"


def _make_client(tmp_path: Path) -> TestClient:
    db = tmp_path / "trader.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    settings = WebSettings(
        admin_user="admin",
        admin_password_hash="$argon2id$dummy",
        session_secret="s",
        db_path=db,
        cookie_secure=False,
    )
    return TestClient(create_app(settings))


def test_static_htmx_served(tmp_path: Path) -> None:
    resp = _make_client(tmp_path).get("/static/htmx.min.js")
    assert resp.status_code == 200
    assert "hx-get" in resp.text  # the shim implements the hx-get attribute


def test_static_css_served(tmp_path: Path) -> None:
    resp = _make_client(tmp_path).get("/static/app.css")
    assert resp.status_code == 200
    assert "badge" in resp.text
