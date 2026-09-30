"""WebSettings.from_env: the UI reads the state DB the trader writes — the mounted config's
observability.db_path unless WEB_DB_PATH overrides it."""

from pathlib import Path

import pytest

from trader.web.settings import WebSettings

BASE = {
    "WEB_ADMIN_USER": "admin",
    "WEB_ADMIN_PASSWORD_HASH": "$argon2id$v=19$m=65536,t=3,p=4$x$y",
    "SESSION_SECRET": "s" * 48,
}
REPO = Path(__file__).parents[3]


def test_db_path_follows_the_mounted_config() -> None:
    env = {**BASE, "WEB_CONFIG_PATH": str(REPO / "config" / "default.yaml")}
    assert WebSettings.from_env(env).db_path == Path("/state/trader.sqlite")


def test_db_path_honors_the_same_env_overrides_as_the_trader() -> None:
    env = {
        **BASE,
        "WEB_CONFIG_PATH": str(REPO / "config" / "default.yaml"),
        "TRADER__OBSERVABILITY__DB_PATH": "/state/other.sqlite",
    }
    assert WebSettings.from_env(env).db_path == Path("/state/other.sqlite")


def test_web_db_path_overrides_the_config() -> None:
    env = {**BASE, "WEB_DB_PATH": "/tmp/x.sqlite", "WEB_CONFIG_PATH": "/does/not/exist.yaml"}
    assert WebSettings.from_env(env).db_path == Path("/tmp/x.sqlite")  # config not even read


def test_unreadable_config_without_override_fails_loudly(tmp_path: Path) -> None:
    env = {**BASE, "WEB_CONFIG_PATH": str(tmp_path / "missing.yaml")}
    with pytest.raises(ValueError, match="WEB_DB_PATH"):
        WebSettings.from_env(env)
