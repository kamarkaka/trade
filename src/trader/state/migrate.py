"""Forward-only SQLite migration runner (design §12).

Applies unapplied ``NNN_*.sql`` files (in filename order), each inside a single
explicit transaction, records it in ``schema_migrations``, and is idempotent on
re-run. Because SQLite supports transactional DDL, a migration that fails partway
is rolled back atomically and can be retried cleanly.

Statements are split on top-level ``;`` (ignoring ``;`` inside string literals and
``--`` / ``/* */`` comments) and run one at a time within the transaction; this is
required because ``sqlite3.executescript`` issues its own COMMIT and would defeat
atomicity. (Trigger bodies, which contain inner ``;``, are not supported by this
splitter — none are used.)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements on top-level ``;``."""
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:  # doubled-quote escape ('' or "")
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":  # line comment → skip to end of line
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and nxt == "*":  # block comment → skip to */
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def run_migrations(conn: sqlite3.Connection, migrations_dir: str | Path = MIGRATIONS_DIR) -> int:
    """Apply pending migrations; return how many were applied (0 if up to date)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    count = 0
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        statements = split_statements(path.read_text(encoding="utf-8"))
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        count += 1
    return count
