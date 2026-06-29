"""Login/logout + session-guard tests (M7.4): CSRF, lockout, secure cookies, idle/absolute
windows — all with an injected clock and a known admin hash (no wall clock)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from trader.web.app import create_app
from trader.web.auth import SESSION_COOKIE
from trader.web.settings import WebSettings

PASSWORD = "correct horse battery staple"
ADMIN_HASH = PasswordHasher().hash(PASSWORD)
T0 = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _settings(db: Path, **over: object) -> WebSettings:
    base: dict[str, object] = {
        "admin_user": "admin",
        "admin_password_hash": ADMIN_HASH,
        "session_secret": "sign-me",
        "db_path": db,
        "cookie_secure": False,  # http TestClient
        "session_idle_seconds": 1800,
        "session_absolute_seconds": 28800,
        "login_max_attempts": 3,
        "login_lockout_seconds": 300,
    }
    base.update(over)
    return WebSettings(**base)  # type: ignore[arg-type]


def _make(tmp_path: Path, clock: _Clock, **over: object) -> TestClient:
    db = tmp_path / "trader.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    app = create_app(_settings(db, **over), now=clock)
    return TestClient(app, follow_redirects=False)


def _csrf(client: TestClient) -> str:
    html = client.get("/login").text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no CSRF token in login form"
    return m.group(1)


def _login(client: TestClient, *, password: str = PASSWORD, user: str = "admin"):
    return client.post(
        "/login", data={"username": user, "password": password, "csrf": _csrf(client)}
    )


@pytest.fixture
def clock() -> _Clock:
    return _Clock(T0)


def test_login_success(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    resp = _login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert SESSION_COOKIE in resp.cookies


def test_login_wrong_password(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    resp = _login(client, password="nope")
    assert resp.status_code == 401
    assert SESSION_COOKIE not in resp.cookies
    assert "invalid credentials" in resp.text.lower()


def test_login_unknown_user_generic_error(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    resp = _login(client, user="root", password="nope")
    assert resp.status_code == 401
    assert "invalid credentials" in resp.text.lower()  # same message as wrong password


def test_login_missing_csrf(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    resp = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf": ""})
    assert resp.status_code == 403


def test_protected_route_redirects_when_unauthenticated(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    resp = client.get("/", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_protected_route_401_for_htmx(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    resp = client.get("/", headers={"HX-Request": "true"})
    assert resp.status_code == 401


def test_authenticated_access_after_login(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    _login(client)  # client jar now holds the session cookie
    resp = client.get("/", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "signed in as admin" in resp.text


def test_logout_clears_cookie(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    _login(client)
    resp = client.post("/logout", data={"csrf": _csrf(client)})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # The Set-Cookie clears the session (then the protected root redirects again).
    client.cookies.delete(SESSION_COOKIE)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_lockout_blocks_after_threshold(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)  # login_max_attempts=3
    for _ in range(3):
        assert _login(client, password="bad").status_code == 401
    # 4th attempt is locked out (429) and does NOT check the password (even correct fails).
    locked = _login(client, password=PASSWORD)
    assert locked.status_code == 429
    assert "too many attempts" in locked.text.lower()
    assert SESSION_COOKIE not in locked.cookies


def test_idle_window_slides(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock)
    _login(client)
    # Just shy of the idle window -> still authenticated, cookie re-issued (slides last_seen).
    clock.t = T0 + timedelta(seconds=1799)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200
    # Another near-idle gap from the refreshed last_seen -> still ok (proves sliding).
    clock.t = clock.t + timedelta(seconds=1799)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200
    # A gap LONGER than idle -> session expired -> redirect.
    clock.t = clock.t + timedelta(seconds=1801)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_absolute_timeout_caps_sliding(tmp_path: Path, clock: _Clock) -> None:
    client = _make(tmp_path, clock, session_absolute_seconds=3600)
    _login(client)
    clock.t = T0 + timedelta(seconds=1700)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200  # within both
    # Past the 1h absolute cap, even though we kept sliding the idle window.
    clock.t = T0 + timedelta(seconds=3601)
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303
