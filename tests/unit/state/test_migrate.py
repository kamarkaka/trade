"""Tests for the SQLite state layer: schema application, idempotency, PRAGMAs,
read-only connection, and foreign-key enforcement."""

import sqlite3
from pathlib import Path

import pytest

from trader.state.db import connect, read_only_connect
from trader.state.migrate import run_migrations

EXPECTED_TABLES = {
    "orders",
    "fills",
    "positions",
    "equity_snapshots",
    "audit_log",
    "daily_counters",
    "kill_switch",
    "schema_migrations",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_applies_initial_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    applied = run_migrations(conn)
    assert applied >= 1
    assert EXPECTED_TABLES <= _tables(conn)


def test_rerun_is_noop(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    assert run_migrations(conn) >= 1
    assert run_migrations(conn) == 0  # idempotent


def test_wal_and_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_read_only_connect_rejects_writes(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite"
    conn = connect(db)
    run_migrations(conn)
    conn.close()  # checkpoint WAL so the file is openable read-only

    ro = read_only_connect(db)
    assert EXPECTED_TABLES <= _tables(ro)  # reads work
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO kill_switch (id, engaged, updated_at) VALUES (1, 1, 'now')")


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fills (client_order_id, symbol, quantity, price, fees, ts, status) "
            "VALUES ('missing-order', 'AAPL', 1, '1', '0', 't', 'FILLED')"
        )


def test_migration_recorded(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert "001_initial.sql" in versions


def test_bad_migration_rolls_back(tmp_path: Path) -> None:
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "001_bad.sql").write_text("CREATE TABLE oops (", encoding="utf-8")  # malformed
    conn = connect(tmp_path / "s.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(conn, bad_dir)
    # nothing recorded → next run can retry cleanly
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
