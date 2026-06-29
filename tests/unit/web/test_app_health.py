"""FastAPI app-skeleton tests (M7.2): /healthz + request-level crash isolation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trader.web.app import create_app
from trader.web.settings import WebSettings


def _settings(db_path: Path) -> WebSettings:
    return WebSettings(
        admin_user="admin",
        admin_password_hash="$argon2id$dummy",  # not exercised in M7.2
        session_secret="test-secret",
        db_path=db_path,
    )


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db = tmp_path / "trader.sqlite"
    _seed_db(db)
    return TestClient(create_app(_settings(db)))


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_db_down(tmp_path: Path) -> None:
    # db_path points at a missing file -> read-only query raises -> 503 (never crashes).
    client = TestClient(create_app(_settings(tmp_path / "missing.sqlite")))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable"}


def test_unhandled_exception_returns_500(tmp_path: Path) -> None:
    db = tmp_path / "trader.sqlite"
    _seed_db(db)
    app = create_app(_settings(db))

    @app.get("/_boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    # raise_server_exceptions=False so the registered handler's 500 is returned (mirrors a
    # real uvicorn worker, which does not propagate the error / crash).
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/_boom")
    assert resp.status_code == 500
    assert "internal error" in resp.text.lower()
    # The process is still alive: a subsequent request still works.
    assert client.get("/healthz").status_code == 200


def test_no_openapi_or_docs(client: TestClient) -> None:
    # Attack surface kept minimal: interactive docs / schema are disabled.
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_secrets_not_in_repr(tmp_path: Path) -> None:
    # The password hash + session secret must never appear in a repr/str (log/traceback leak).
    settings = WebSettings(
        admin_user="admin",
        admin_password_hash="$argon2id$SUPERSECRETHASH",
        session_secret="SUPERSECRETKEY",
        db_path=tmp_path / "x.sqlite",
    )
    for blob in (repr(settings), str(settings)):
        assert "SUPERSECRETHASH" not in blob
        assert "SUPERSECRETKEY" not in blob
    # but the values are still retrievable for verification/signing
    assert settings.admin_password_hash.get_secret_value() == "$argon2id$SUPERSECRETHASH"
    assert settings.session_secret.get_secret_value() == "SUPERSECRETKEY"
