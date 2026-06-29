"""Token-store file-permission + git-tracking guards (M1.10, design §13).

The OAuth token store holds the access/refresh tokens, so the file must be created
with owner-only (0600) permissions and must never live on a git-tracked path.
"""

from __future__ import annotations

import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_token_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "token.sqlite"
    store = TokenStore(path)  # created on init
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    # still 0600 after a write
    store.save(TokenSet("ACC", "REF", NOW + timedelta(seconds=1800), NOW))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_token_file_not_group_or_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "token.sqlite"
    TokenStore(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0  # no group/other bits


def test_gitignore_excludes_token_and_secret_paths() -> None:
    """The default token-store location and secret material are never git-tracked."""
    gitignore = (Path(__file__).resolve().parents[3] / ".gitignore").read_text()
    for pattern in ("/state/", "*.sqlite", ".env", "*.pem", "*.key", "token*.json"):
        assert pattern in gitignore, f"missing .gitignore pattern: {pattern!r}"
