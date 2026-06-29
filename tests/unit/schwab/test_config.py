"""Tests for SchwabClientConfig + constants."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import API_BASE, OAUTH_TOKEN_URL


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
