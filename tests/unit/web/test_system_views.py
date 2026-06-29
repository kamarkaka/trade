"""System / Schedule / Re-auth view tests (M7.7): authenticated, seeded DB, injected clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trader.state.db import connect
from trader.state.migrate import run_migrations
from trader.web.app import create_app
from trader.web.db import ReadOnlyStateDB
from trader.web.repository import MonitoringRepo
from trader.web.security import make_session_token
from trader.web.settings import WebSettings

SECRET = "view-secret"
NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-TOKEN-never-render"


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _seed(db: Path, *, heartbeat_at: datetime, kill_engaged: bool = False) -> None:
    conn = connect(db)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO heartbeat (id, last_alive_at, scheduler_state, detail) VALUES (1,?,?,?)",
        (heartbeat_at.isoformat(), "running", "3 jobs"),
    )
    conn.execute(
        "INSERT INTO kill_switch (id, engaged, reason, source, updated_at) VALUES (1,?,?,?,?)",
        (1 if kill_engaged else 0, "manual halt" if kill_engaged else None, "cli", NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO fired_slot (slot_date, strategy_id, slot_id, status, planned_fire_ts, "
        "drift_seconds, seed, finished_at) VALUES "
        "('2026-06-29','momentum','open','done',?,137,7,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO fired_slot (slot_date, strategy_id, slot_id, status, planned_fire_ts, "
        "drift_seconds, seed, error) VALUES "
        "('2026-06-29','meanrev','mid','failed',?,0,7,'boom')",
        (NOW.isoformat(),),
    )
    conn.commit()
    conn.close()


def _seed_token(db: Path, *, refresh_age_days: float) -> None:
    import sqlite3

    tok = db.parent / "schwab_token.sqlite"
    conn = sqlite3.connect(tok)
    conn.execute(
        "CREATE TABLE tokens (id INTEGER PRIMARY KEY, access_token TEXT, refresh_token TEXT, "
        "access_expires_at TEXT, refresh_issued_at TEXT, scope TEXT)"
    )
    conn.execute(
        "INSERT INTO tokens VALUES (1, ?, ?, ?, ?, 's')",
        (
            SENTINEL,
            SENTINEL,
            (NOW + timedelta(minutes=18)).isoformat(),
            (NOW - timedelta(days=refresh_age_days)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _build(
    tmp_path: Path,
    *,
    now: datetime = NOW,
    heartbeat_at: datetime = NOW,
    kill_engaged: bool = False,
    refresh_age_days: float | None = 1.0,
    mode: str = "paper",
    authed: bool = True,
) -> TestClient:
    db = tmp_path / "trader.sqlite"
    _seed(db, heartbeat_at=heartbeat_at, kill_engaged=kill_engaged)
    if refresh_age_days is not None:
        _seed_token(db, refresh_age_days=refresh_age_days)
    settings = WebSettings(
        admin_user="admin",
        admin_password_hash="$argon2id$dummy",
        session_secret=SECRET,
        db_path=db,
        cookie_secure=False,
    )
    clock = _Clock(now)
    app = create_app(settings, now=clock)
    # Controlled config_loader (real config file is absent in tests).
    app.state.repo = MonitoringRepo(
        ReadOnlyStateDB(db),
        config_loader=lambda: {"mode": mode, "alerting": {"heartbeat_minutes": 60}},
        token_store_path=db.parent / "schwab_token.sqlite",
    )
    client = TestClient(app, follow_redirects=False)
    if authed:
        client.cookies.set("session", make_session_token(SECRET, "admin", now))
    return client


def test_system_requires_auth(tmp_path: Path) -> None:
    client = _build(tmp_path, authed=False)
    assert client.get("/system", headers={"accept": "text/html"}).status_code == 303
    assert client.get("/system", headers={"HX-Request": "true"}).status_code == 401


def test_system_shows_mode_and_killswitch(tmp_path: Path) -> None:
    client = _build(tmp_path, kill_engaged=True, mode="paper")
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "mode: paper" in resp.text  # base chrome
    assert "ENGAGED" in resp.text and "manual halt" in resp.text


def test_heartbeat_stale_badge(tmp_path: Path) -> None:
    # Heartbeat 1 day old, threshold = 2*60min = 2h -> stale.
    client = _build(tmp_path, heartbeat_at=NOW - timedelta(days=1))
    assert "STALE" in client.get("/system").text


def test_heartbeat_healthy_badge(tmp_path: Path) -> None:
    client = _build(tmp_path, heartbeat_at=NOW - timedelta(minutes=5))
    body = client.get("/system").text
    assert "healthy" in body and "STALE" not in body


def test_token_countdown_no_token_value(tmp_path: Path) -> None:
    resp = _build(tmp_path, refresh_age_days=1.0).get("/system")
    assert "days" in resp.text
    assert SENTINEL not in resp.text  # never the token value


def test_system_fragment_returns_partial(tmp_path: Path) -> None:
    resp = _build(tmp_path).get("/system/fragment", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<html" not in resp.text.lower()  # inner partial only
    assert "Mode" in resp.text


def test_schedule_shows_drift_and_skips(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/schedule").text
    assert "momentum" in body and "meanrev" in body
    assert "137" in body  # realized drift seconds
    assert "failed" in body  # the failed slot surfaced


def test_reauth_displays_runbook_no_execute(tmp_path: Path) -> None:
    body = _build(tmp_path).get("/reauth").text
    assert "trader reauth" in body
    assert "executes nothing" in body
    # Display-only: the ONLY form/button on the page is the chrome's logout (no re-auth action).
    assert body.count("<form") == 1
    assert 'action="/logout"' in body
    assert body.count("<button") == 1  # the "Sign out" button


@pytest.mark.parametrize(
    ("age_days", "level_class"),
    [
        (1.0, "badge-ok"),  # 6 days left -> green
        (5.5, "badge-warn"),  # 1.5 days left -> amber
        (8.0, "badge-alert"),  # expired -> red
    ],
)
def test_token_badge_thresholds(tmp_path: Path, age_days: float, level_class: str) -> None:
    body = _build(tmp_path, refresh_age_days=age_days).get("/system").text
    assert level_class in body
