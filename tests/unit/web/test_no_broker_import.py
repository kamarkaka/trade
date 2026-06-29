"""Guard: trader.web imports NO broker/credential code path, and its DB handle is read-only
(M7.10 / §17 exit criteria). The forbidden-module list lives here so a future route can't
silently add a broker path."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trader.web.app import create_app
from trader.web.settings import WebSettings

# Modules the read-only monitoring service must never pull in (broker order path, Schwab
# client, execution/idempotency, OAuth/credential store). Note trader.web.auth is the web's
# OWN session guard — distinct from trader.auth (the OAuth package).
FORBIDDEN = ("trader.broker", "trader.schwab", "trader.execution", "trader.auth")


def test_web_does_not_import_broker_or_schwab() -> None:
    # Import the whole web package in a CLEAN subprocess and assert no forbidden module loaded
    # (catches transitive + would-be lazy module-level imports).
    probe = (
        "import sys\n"
        "import trader.web\n"
        "from trader.web import app, auth, db, repository, security, settings, templating, views\n"
        "from trader.web.routes import (auth_routes, system_routes, strategy_routes, "
        "account_routes, orders_routes, alerts_routes, config_routes)\n"
        f"bad = sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r}))\n"
        "print(';'.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = [m for m in result.stdout.strip().split(";") if m]
    assert leaked == [], f"trader.web pulled in forbidden modules: {leaked}"


def test_app_db_handle_is_readonly(tmp_path: Path) -> None:
    db = tmp_path / "trader.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeat (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    app = create_app(
        WebSettings(
            admin_user="admin",
            admin_password_hash="$argon2id$dummy",
            session_secret="s",
            db_path=db,
            cookie_secure=False,
        )
    )
    with app.state.db.connect() as ro, pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO heartbeat (id) VALUES (1)")
