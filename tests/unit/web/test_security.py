"""Auth-primitive tests (M7.3): argon2id verify, signed stateless session + CSRF, lockout.

All time-dependent logic takes an injected ``now`` — no wall clock — so expiry/lockout are
deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher

from trader.web.security import (
    LoginThrottle,
    make_csrf_token,
    make_session_token,
    read_session_token,
    refresh_session_token,
    validate_csrf,
    verify_password,
)

SECRET = "test-session-secret"
NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
IDLE = 1800
ABS = 28800


def test_verify_correct_password() -> None:
    h = PasswordHasher().hash("s3cret")
    assert verify_password("s3cret", h) is True


def test_verify_wrong_password() -> None:
    h = PasswordHasher().hash("s3cret")
    assert verify_password("nope", h) is False


def test_verify_malformed_hash_is_false() -> None:
    assert verify_password("x", "not-a-real-hash") is False


def test_verify_none_inputs_are_false() -> None:
    # Defensive: non-str inputs must fail closed, never raise (the module never raises).
    assert verify_password(None, "x") is False  # type: ignore[arg-type]
    assert verify_password("x", None) is False  # type: ignore[arg-type]


def test_session_naive_now_returns_none_not_crash() -> None:
    # A naive `now` (caller bug) must yield None, NOT an aware/naive subtraction TypeError.
    token = make_session_token(SECRET, "admin", NOW)
    naive_now = datetime(2026, 6, 29, 12, 0)
    assert (
        read_session_token(SECRET, token, naive_now, idle_seconds=IDLE, absolute_seconds=ABS)
        is None
    )


def test_refresh_slides_idle_but_preserves_absolute() -> None:
    token = make_session_token(SECRET, "admin", NOW)
    # Refresh near the idle edge: new token is valid, but the ORIGINAL issued_at still binds.
    mid = NOW + timedelta(seconds=IDLE - 1)
    refreshed = refresh_session_token(SECRET, token, mid, idle_seconds=IDLE, absolute_seconds=ABS)
    assert refreshed is not None
    # The refreshed token reads fine just after `mid` (idle slid forward)...
    soon = mid + timedelta(seconds=10)
    assert (
        read_session_token(SECRET, refreshed, soon, idle_seconds=IDLE, absolute_seconds=ABS)
        == "admin"
    )
    # ...but past the ORIGINAL absolute cap it dies even though last_seen is recent.
    past_abs = NOW + timedelta(seconds=ABS + 1)
    assert (
        read_session_token(SECRET, refreshed, past_abs, idle_seconds=IDLE, absolute_seconds=ABS)
        is None
    )


def test_refresh_invalid_token_returns_none() -> None:
    assert (
        refresh_session_token(SECRET, "garbage", NOW, idle_seconds=IDLE, absolute_seconds=ABS)
        is None
    )


def test_session_roundtrip() -> None:
    token = make_session_token(SECRET, "admin", NOW)
    assert (
        read_session_token(SECRET, token, NOW, idle_seconds=IDLE, absolute_seconds=ABS) == "admin"
    )


def test_session_idle_expiry() -> None:
    token = make_session_token(SECRET, "admin", NOW)
    later = NOW + timedelta(seconds=IDLE + 1)
    assert read_session_token(SECRET, token, later, idle_seconds=IDLE, absolute_seconds=ABS) is None


def test_session_absolute_expiry() -> None:
    # Within idle but past absolute -> None. Mint with a recent last_seen but old issued_at.
    token = make_session_token(SECRET, "admin", NOW)
    # last_seen == issued_at == NOW; jump just past absolute (also past idle, so refresh
    # last_seen by re-reading is moot — absolute is the binding cap here).
    later = NOW + timedelta(seconds=ABS + 1)
    assert (
        read_session_token(SECRET, token, later, idle_seconds=ABS + 100, absolute_seconds=ABS)
        is None
    )


def test_session_tampered_returns_none() -> None:
    token = make_session_token(SECRET, "admin", NOW)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert (
        read_session_token(SECRET, tampered, NOW, idle_seconds=IDLE, absolute_seconds=ABS) is None
    )


def test_session_wrong_secret_returns_none() -> None:
    token = make_session_token(SECRET, "admin", NOW)
    assert read_session_token("other", token, NOW, idle_seconds=IDLE, absolute_seconds=ABS) is None


def test_csrf_validate() -> None:
    token = make_csrf_token(SECRET, NOW)
    assert validate_csrf(SECRET, token) is True
    assert validate_csrf(SECRET, token[:-1] + "Z") is False  # forged
    assert validate_csrf("other", token) is False  # wrong secret
    assert validate_csrf(SECRET, "") is False


def test_lockout_after_max_attempts() -> None:
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=300)
    key = LoginThrottle.key("admin", "1.2.3.4")
    assert throttle.is_locked(key, NOW) is False
    for _ in range(3):
        throttle.record_failure(key, NOW)
    assert throttle.is_locked(key, NOW) is True
    # After the lockout window passes, old failures age out -> unlocked.
    assert throttle.is_locked(key, NOW + timedelta(seconds=301)) is False


def test_lockout_success_clears_failures() -> None:
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=300)
    key = LoginThrottle.key("admin", "1.2.3.4")
    throttle.record_failure(key, NOW)
    throttle.record_failure(key, NOW)
    throttle.record_success(key)
    throttle.record_failure(key, NOW)
    assert throttle.is_locked(key, NOW) is False  # only 1 failure after the reset


def test_lockout_is_per_key() -> None:
    throttle = LoginThrottle(max_attempts=2, lockout_seconds=300)
    a = LoginThrottle.key("admin", "1.1.1.1")
    b = LoginThrottle.key("admin", "2.2.2.2")
    throttle.record_failure(a, NOW)
    throttle.record_failure(a, NOW)
    assert throttle.is_locked(a, NOW) is True
    assert throttle.is_locked(b, NOW) is False  # a different IP is unaffected
