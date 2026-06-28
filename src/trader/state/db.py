"""SQLite connection helpers (design §3/§12).

WAL mode lets the future read-only web reader (M7) read concurrently with the
daemon writer; ``busy_timeout`` absorbs brief write contention; foreign keys are
enforced. Money is stored as TEXT (Decimal string) by callers to avoid binary
floats.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a read/write connection with WAL, a busy timeout, and FK enforcement."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def read_only_connect(path: str | Path) -> sqlite3.Connection:
    """Open a strictly read-only connection (``mode=ro`` + ``query_only``).

    Used by the read-only web UI (M7); any write attempt raises.
    """
    uri = f"file:{Path(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn
