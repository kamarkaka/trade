"""Tests for the SQLite TokenStore: roundtrip, empty load, perms, corruption."""

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.schwab.errors import SchwabBadResponseError

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def _tokens() -> TokenSet:
    return TokenSet(
        access_token="access-xyz",
        refresh_token="refresh-xyz",
        access_token_expires_at=NOW + timedelta(minutes=30),
        refresh_token_issued_at=NOW,
        scope="api",
    )


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.sqlite")
    store.save(_tokens())
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access-xyz"
    assert loaded.refresh_token == "refresh-xyz"
    assert loaded.access_token_expires_at == NOW + timedelta(minutes=30)
    assert loaded.refresh_token_issued_at == NOW
    assert loaded.scope == "api"


def test_load_empty_returns_none(tmp_path: Path) -> None:
    assert TokenStore(tmp_path / "token.sqlite").load() is None


def test_save_overwrites_single_row(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.sqlite")
    store.save(_tokens())
    store.save(TokenSet("a2", "r2", NOW + timedelta(minutes=30), NOW))
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "a2"


def test_clear(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.sqlite")
    store.save(_tokens())
    store.clear()
    assert store.load() is None


def test_file_permissions_are_restrictive(tmp_path: Path) -> None:
    path = tmp_path / "token.sqlite"
    store = TokenStore(path)
    store.save(_tokens())
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_corrupt_timestamps_raise(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "token.sqlite"
    store = TokenStore(path)
    store.save(_tokens())
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE tokens SET access_expires_at = 'not-a-date' WHERE id = 1")
        conn.commit()
    with pytest.raises(SchwabBadResponseError):
        store.load()
