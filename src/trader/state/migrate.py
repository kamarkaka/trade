"""Forward-only SQLite migration runner (design §12).

Applies unapplied ``NNN_*.sql`` files (in filename order) inside a transaction,
records each in ``schema_migrations``, and is idempotent on re-run.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations(conn: sqlite3.Connection, migrations_dir: str | Path = MIGRATIONS_DIR) -> int:
    """Apply pending migrations; return how many were applied (0 if up to date)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    count = 0
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        count += 1
    return count
