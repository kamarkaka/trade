"""Read-only state-DB handle tests (M7.1): serves parameterized reads, rejects ALL writes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trader.web.db import ReadOnlyStateDB


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, symbol TEXT, qty INTEGER)")
        conn.execute("INSERT INTO orders (symbol, qty) VALUES ('AAPL', 10), ('MSFT', 5)")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def ro_db(tmp_path: Path) -> ReadOnlyStateDB:
    db = tmp_path / "trader.sqlite"
    _seed(db)
    return ReadOnlyStateDB(db)


def test_select_returns_rows(ro_db: ReadOnlyStateDB) -> None:
    rows = ro_db.query("SELECT symbol, qty FROM orders ORDER BY symbol")
    assert [(r["symbol"], r["qty"]) for r in rows] == [("AAPL", 10), ("MSFT", 5)]


def test_query_is_parameterized(ro_db: ReadOnlyStateDB) -> None:
    row = ro_db.query_one("SELECT qty FROM orders WHERE symbol = ?", ("MSFT",))
    assert row is not None and row["qty"] == 5
    assert ro_db.query_one("SELECT 1 FROM orders WHERE symbol = ?", ("NOPE",)) is None


def test_pragma_query_only_set(ro_db: ReadOnlyStateDB) -> None:
    with ro_db.connect() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_insert_raises(ro_db: ReadOnlyStateDB) -> None:
    with ro_db.connect() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO orders (symbol, qty) VALUES ('TSLA', 1)")


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO orders (symbol, qty) VALUES ('X', 1)",
        "UPDATE orders SET qty = 99 WHERE symbol = 'AAPL'",
        "DELETE FROM orders WHERE symbol = 'AAPL'",
        "CREATE TABLE evil (x INTEGER)",
        "DROP TABLE orders",
    ],
)
def test_all_writes_raise(ro_db: ReadOnlyStateDB, stmt: str) -> None:
    with ro_db.connect() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute(stmt)


def test_attach_cannot_create_writable_db(ro_db: ReadOnlyStateDB, tmp_path: Path) -> None:
    # ATTACH must not become a write side-channel: either it is refused, or the attached DB
    # is itself query_only so a write into it still raises.
    side = tmp_path / "side.sqlite"
    with ro_db.connect() as conn:
        try:
            conn.execute("ATTACH DATABASE ? AS evil", (str(side),))
        except sqlite3.OperationalError:
            return  # refused outright — good
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil.t (x INTEGER)")


def test_missing_db_raises(tmp_path: Path) -> None:
    db = ReadOnlyStateDB(tmp_path / "does_not_exist.sqlite")
    with pytest.raises(FileNotFoundError, match="state DB not found"):
        db.query("SELECT 1")


def test_concurrent_wal_read_succeeds(tmp_path: Path) -> None:
    # A WAL-mode DB with an open writer must still serve read-only reads (busy_timeout).
    db = tmp_path / "trader.sqlite"
    _seed(db)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO orders (symbol, qty) VALUES ('NVDA', 7)")
        writer.commit()
        ro = ReadOnlyStateDB(db)
        rows = ro.query("SELECT COUNT(*) AS n FROM orders")
        assert rows[0]["n"] == 3
    finally:
        writer.close()
