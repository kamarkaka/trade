"""SQLite connection helpers (design §3/§12).

WAL mode lets the future read-only web reader (M7) read concurrently with the
daemon writer; ``busy_timeout`` absorbs brief write contention; foreign keys are
enforced. Connections run in **autocommit** mode (``isolation_level=None``) so
transactions are controlled explicitly (e.g. the migration runner) rather than by
sqlite3's implicit management. Money is stored as TEXT (Decimal string) by callers
to avoid binary floats.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a read/write connection with WAL, a busy timeout, and FK enforcement.

    ``check_same_thread=False`` so the connection (created on the main thread) is usable
    from the scheduler's worker thread (M3.11). Concurrent use is avoided by design — the
    daemon runs jobs on a single-worker executor and the global cycle lock serializes the
    decision->submit critical section — so this only relaxes the thread-identity check.
    """
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False
    )  # autocommit; explicit BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def read_only_connect(path: str | Path) -> sqlite3.Connection:
    """Open a strictly read-only connection (``mode=ro`` + ``query_only``).

    Used by the read-only web UI (M7); any write attempt raises. The path is
    encoded via ``as_uri()`` so spaces/special characters are handled safely.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn
