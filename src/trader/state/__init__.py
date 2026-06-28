"""Durable state: SQLite connections + the migration runner (design §12).

Per-milestone tables are added via numbered migrations under ``migrations/``.
"""

from __future__ import annotations

from .db import connect, read_only_connect
from .migrate import MIGRATIONS_DIR, run_migrations

__all__ = ["MIGRATIONS_DIR", "connect", "read_only_connect", "run_migrations"]
