"""Tests for SchwabClientConfig + constants."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from trader.schwab.config import SchwabClientConfig, schwab_config_from_env
from trader.schwab.constants import API_BASE, OAUTH_TOKEN_URL
from trader.schwab.errors import SchwabAuthError


def _cfg(**kw: object) -> SchwabClientConfig:
    base: dict[str, object] = {
        "app_key": "key",
        "app_secret": "secret",
        "token_store_path": Path("/tmp/token.sqlite"),
    }
    base.update(kw)
    return SchwabClientConfig(**base)  # type: ignore[arg-type]


def test_constants_present() -> None:
    assert API_BASE == "https://api.schwabapi.com"
    assert OAUTH_TOKEN_URL.startswith(API_BASE)


def test_redirect_uri_must_be_https() -> None:
    with pytest.raises(ValidationError):
        _cfg(redirect_uri="http://127.0.0.1:8182")
    assert _cfg(redirect_uri="https://127.0.0.1:8182").redirect_uri.startswith("https://")


def test_rate_limit_ceiling() -> None:
    with pytest.raises(ValidationError):
        _cfg(rate_limit_per_min=200)
    assert _cfg(rate_limit_per_min=120).rate_limit_per_min == 120


def test_app_secret_not_in_repr() -> None:
    cfg = _cfg(app_secret="topsecretvalue")
    assert "topsecretvalue" not in repr(cfg)
    assert cfg.app_secret.get_secret_value() == "topsecretvalue"


def test_defaults() -> None:
    cfg = _cfg()
    assert cfg.refresh_token_max_age_days == 7
    assert cfg.max_retries == 4
    assert cfg.request_timeout_seconds == 30.0


# --- schwab_config_from_env ------------------------------------------------- #


def test_from_env_reads_credentials_and_overrides() -> None:
    env = {
        "SCHWAB_APP_KEY": "K",
        "SCHWAB_APP_SECRET": "S",
        "SCHWAB_REDIRECT_URI": "https://127.0.0.1:9999",
        "SCHWAB_TOKEN_STORE_PATH": "/custom/token.sqlite",
    }
    cfg = schwab_config_from_env(
        default_token_store="/state/schwab_token.sqlite",
        rate_limit_per_min=80,
        environ=env,
        require_credentials=True,
    )
    assert cfg.app_key == "K"
    assert cfg.app_secret.get_secret_value() == "S"
    assert cfg.redirect_uri == "https://127.0.0.1:9999"
    assert cfg.token_store_path == Path("/custom/token.sqlite")
    assert cfg.rate_limit_per_min == 80


def test_from_env_defaults_token_store_when_unset() -> None:
    cfg = schwab_config_from_env(default_token_store="/state/schwab_token.sqlite", environ={})
    assert cfg.token_store_path == Path("/state/schwab_token.sqlite")
    assert cfg.app_key == ""  # absent creds tolerated when not required


def test_from_env_requires_credentials_when_asked() -> None:
    with pytest.raises(SchwabAuthError):
        schwab_config_from_env(
            default_token_store="/state/t.sqlite", environ={}, require_credentials=True
        )


def test_from_env_secret_not_in_repr() -> None:
    cfg = schwab_config_from_env(
        default_token_store="/state/t.sqlite",
        environ={"SCHWAB_APP_KEY": "K", "SCHWAB_APP_SECRET": "leakme"},
    )
    assert "leakme" not in repr(cfg)
