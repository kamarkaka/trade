"""Tests for the Schwab error taxonomy."""

from trader.schwab.errors import (
    SchwabAuthError,
    SchwabError,
    SchwabRefreshTokenDeadError,
    SchwabTokenExpiredError,
)


def test_error_hierarchy() -> None:
    assert issubclass(SchwabAuthError, SchwabError)
    assert issubclass(SchwabTokenExpiredError, SchwabAuthError)
    assert issubclass(SchwabRefreshTokenDeadError, SchwabAuthError)


def test_status_code_carried() -> None:
    err = SchwabError("boom", status_code=429)
    assert err.status_code == 429
    assert str(err) == "boom"
    assert SchwabError().status_code is None
