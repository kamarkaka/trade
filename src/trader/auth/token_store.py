"""Durable OAuth token storage (design §8.2 / §13).

Tokens live in their own single-row SQLite file (separate from the trading state
DB) with restrictive 0600 permissions. Datetimes are stored as ISO-8601 UTC
strings. Rollback-journal mode (not WAL) keeps it to a single file to lock down.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from trader.schwab.errors import SchwabBadResponseError

from .tokens import TokenSet

_CREATE = (
    "CREATE TABLE IF NOT EXISTS tokens ("
    "id INTEGER PRIMARY KEY CHECK (id = 1), "
    "access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, "
    "access_expires_at TEXT NOT NULL, refresh_issued_at TEXT NOT NULL, "
    "scope TEXT, updated_at TEXT NOT NULL)"
)


class TokenStore:
    """Persists a single TokenSet; restrictive file permissions; secret-safe."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as conn:
            conn.execute(_CREATE)
        self._restrict_perms()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _restrict_perms(self) -> None:
        # best-effort (e.g. on platforms without POSIX perms)
        with contextlib.suppress(OSError):
            self._path.chmod(0o600)

    def save(self, tokens: TokenSet) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tokens "
                "(id, access_token, refresh_token, access_expires_at, refresh_issued_at, "
                "scope, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    tokens.access_token,
                    tokens.refresh_token,
                    tokens.access_token_expires_at.isoformat(),
                    tokens.refresh_token_issued_at.isoformat(),
                    tokens.scope,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self._restrict_perms()

    def load(self) -> TokenSet | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT access_token, refresh_token, access_expires_at, refresh_issued_at, scope "
                "FROM tokens WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            return TokenSet(
                access_token=row["access_token"],
                refresh_token=row["refresh_token"],
                access_token_expires_at=datetime.fromisoformat(row["access_expires_at"]),
                refresh_token_issued_at=datetime.fromisoformat(row["refresh_issued_at"]),
                scope=row["scope"],
            )
        except ValueError as exc:
            raise SchwabBadResponseError("corrupt token timestamps in store") from exc

    def clear(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute("DELETE FROM tokens")
