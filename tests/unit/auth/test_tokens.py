"""Tests for TokenSet age/expiry logic (deterministic via FakeClock)."""

from datetime import UTC, datetime, timedelta

import pytest

from fakes import FakeClock
from trader.auth.tokens import TokenSet

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def _tokens(*, access_in_s: int = 1800, refresh_age_days: float = 0.0) -> TokenSet:
    return TokenSet(
        access_token="a",
        refresh_token="r",
        access_token_expires_at=NOW + timedelta(seconds=access_in_s),
        refresh_token_issued_at=NOW - timedelta(days=refresh_age_days),
        scope="api",
    )


def test_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        TokenSet("a", "r", datetime(2026, 6, 28, 12, 0), NOW)  # naive access expiry


def test_access_expired_with_skew() -> None:
    clock = FakeClock(NOW)
    # expires 30s from now; with 60s skew it is considered expired
    assert _tokens(access_in_s=30).access_expired(clock, skew_seconds=60) is True
    # expires 600s from now → not expired
    assert _tokens(access_in_s=600).access_expired(clock, skew_seconds=60) is False


def test_refresh_age_days() -> None:
    clock = FakeClock(NOW)
    assert _tokens(refresh_age_days=6.5).refresh_age_days(clock) == pytest.approx(6.5)


def test_refresh_alert_due() -> None:
    clock = FakeClock(NOW)
    tok = _tokens(refresh_age_days=5.0)
    assert tok.refresh_alert_due(clock, max_age_days=7, lead_days=2) is True
    assert (
        _tokens(refresh_age_days=4.9).refresh_alert_due(clock, max_age_days=7, lead_days=2) is False
    )


def test_refresh_expired() -> None:
    clock = FakeClock(NOW)
    assert _tokens(refresh_age_days=7.01).refresh_expired(clock, max_age_days=7) is True
    assert _tokens(refresh_age_days=6.99).refresh_expired(clock, max_age_days=7) is False


def test_repr_and_str_mask_tokens() -> None:
    tok = TokenSet("ACCESS_SECRET", "REFRESH_SECRET", NOW, NOW, scope="api")
    for rendered in (repr(tok), str(tok), f"{tok}"):
        assert "ACCESS_SECRET" not in rendered
        assert "REFRESH_SECRET" not in rendered
        assert "***" in rendered
