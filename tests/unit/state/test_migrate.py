"""Tests for the SQLite state layer: schema application, idempotency, PRAGMAs,
read-only connection, and foreign-key enforcement."""

import sqlite3
from pathlib import Path

import pytest

from trader.state.db import connect, read_only_connect
from trader.state.migrate import run_migrations, split_statements

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
    assert _tables(conn) >= EXPECTED_TABLES


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
    assert _tables(ro) >= EXPECTED_TABLES  # reads work
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
    versions = [
        r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    assert "001_initial.sql" in versions


def test_partial_migration_failure_is_atomic(tmp_path: Path) -> None:
    mig = tmp_path / "migrations"
    mig.mkdir()
    # first statement valid, second invalid → the whole migration must roll back
    (mig / "001_x.sql").write_text("CREATE TABLE good (x);\nCREATE TABLE bad (;", encoding="utf-8")
    conn = connect(tmp_path / "s.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(conn, mig)
    assert "good" not in _tables(conn)  # rolled back, not left half-applied
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0

    # a corrected migration then applies cleanly (proves the failure wasn't stuck)
    (mig / "001_x.sql").write_text(
        "CREATE TABLE good (x);\nCREATE TABLE better (y);", encoding="utf-8"
    )
    assert run_migrations(conn, mig) == 1
    assert {"good", "better"} <= _tables(conn)


def test_split_statements_ignores_semicolons_in_strings_and_comments() -> None:
    sql = """
    -- a comment with ; inside
    CREATE TABLE t (a TEXT DEFAULT 'x;y');
    /* block ; comment */
    INSERT INTO t (a) VALUES ('p;q');
    """
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE t")
    assert "p;q" in stmts[1]


def test_read_only_connect_handles_special_path(tmp_path: Path) -> None:
    sub = tmp_path / "a b#c"  # space + hash that would break naive file: URI building
    sub.mkdir()
    db = sub / "s.sqlite"
    conn = connect(db)
    run_migrations(conn)
    conn.close()
    ro = read_only_connect(db)
    assert _tables(ro) >= EXPECTED_TABLES
