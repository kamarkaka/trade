"""Read-only access to the trading state DB for the monitoring UI (design §19, M7.1).

``ReadOnlyStateDB`` opens the SQLite state DB with a guaranteed read-only handle — a URI
``mode=ro`` connection PLUS ``PRAGMA query_only=ON`` (belt-and-suspenders) — so any accidental
write raises ``sqlite3.OperationalError`` rather than mutating trading state. It serves only
PARAMETERIZED reads; there is no write method anywhere on the class.

Stdlib ``sqlite3`` only — this module imports nothing from ``trader.broker/schwab/execution/
auth`` (web isolation). ``busy_timeout`` lets reads wait out the daemon's brief WAL write
locks instead of failing. ``immutable`` is left OFF (default) because the daemon is actively
writing the -wal/-shm sidecars.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

# Parameter values accepted by parameterized queries (sqlite3-bindable scalars).
QueryParams = Sequence[object]


class ReadOnlyStateDB:
    """A read-only, query-only SQLite handle over the trading state DB."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(db_path)
        self._busy_timeout_ms = int(busy_timeout_ms)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a fresh read-only connection (closed on exit).

        Raises ``FileNotFoundError`` if the DB file is missing (surfaced as a 503 by the web
        layer) — checked explicitly so the failure is clear rather than a cryptic sqlite
        "unable to open database file"."""
        if not self._path.exists():
            raise FileNotFoundError(f"state DB not found: {self._path}")
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")  # reject writes even if mode=ro is bypassed
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            yield conn
        finally:
            conn.close()

    def query(self, sql: str, params: QueryParams = ()) -> list[sqlite3.Row]:
        """Run a parameterized SELECT and return all rows. Pass values via ``params`` —
        NEVER interpolate them into ``sql`` (SQL injection / quoting bugs)."""
        with self.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: QueryParams = ()) -> sqlite3.Row | None:
        """Run a parameterized SELECT and return the first row (or ``None``)."""
        with self.connect() as conn:
            row: sqlite3.Row | None = conn.execute(sql, tuple(params)).fetchone()
            return row


__all__ = ["QueryParams", "ReadOnlyStateDB"]
