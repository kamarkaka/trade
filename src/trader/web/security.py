"""Auth primitives for the monitoring UI (design §19.5, M7.3).

Route-independent security building blocks:
  * ``verify_password`` — argon2id verify (constant-time; mismatch -> False, never raises).
  * stateless signed SESSION token — the signed payload IS the session (§12: the web service
    writes NOTHING, so there is no server-side session table). Idle + absolute expiry are
    enforced from ``issued_at``/``last_seen`` in the payload against an INJECTED ``now`` (no
    wall clock), so expiry is deterministically testable; the route layer (M7.4) refreshes
    ``last_seen`` by re-issuing the cookie on each authenticated request.
  * signed CSRF token (double-submit, alongside SameSite=strict) for the login/logout POSTs.
  * ``LoginThrottle`` — in-memory per (user, client-ip) failure tracker with a lockout window
    (process-local; resets on restart — acceptable for a single admin).

A signed (URLSafeSerializer) token, NOT a time-stamped one, is used so token bytes carry no
hidden wall-clock and all time logic flows through the injected ``now``.
"""

from __future__ import annotations

from datetime import datetime

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from itsdangerous import BadSignature, URLSafeSerializer

_SESSION_SALT = "trader-web-session"
_CSRF_SALT = "trader-web-csrf"

# One hasher instance (argon2id defaults). Verification is constant-time within argon2.
_HASHER = PasswordHasher()


def verify_password(plain: str, stored_hash: str) -> bool:
    """True iff ``plain`` matches the argon2id ``stored_hash``. Never raises — any
    mismatch / malformed hash returns False (fail closed)."""
    try:
        return _HASHER.verify(stored_hash, plain)
    except (Argon2Error, InvalidHashError):  # mismatch OR malformed/unparseable hash
        return False


# --------------------------------------------------------------------------- #
# Session token (stateless, signed)                                           #
# --------------------------------------------------------------------------- #


def _session_serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt=_SESSION_SALT)


def make_session_token(secret: str, username: str, now: datetime) -> str:
    """Mint a signed session token stamped issued_at = last_seen = ``now``."""
    payload = {"user": username, "issued_at": now.isoformat(), "last_seen": now.isoformat()}
    return _session_serializer(secret).dumps(payload)


def read_session_token(
    secret: str,
    token: str,
    now: datetime,
    *,
    idle_seconds: int,
    absolute_seconds: int,
) -> str | None:
    """Return the username if ``token`` is validly signed AND within both the idle window
    (``now - last_seen``) and the absolute window (``now - issued_at``); else ``None``
    (tampered signature, malformed payload, or expired)."""
    try:
        data = _session_serializer(secret).loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    try:
        issued_at = datetime.fromisoformat(data["issued_at"])
        last_seen = datetime.fromisoformat(data["last_seen"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(user, str):
        return None
    # Negative deltas (token from the "future" under clock skew) are treated as fresh.
    if (now - last_seen).total_seconds() > idle_seconds:
        return None
    if (now - issued_at).total_seconds() > absolute_seconds:
        return None
    return user


# --------------------------------------------------------------------------- #
# CSRF token (signed; double-submit alongside SameSite=strict)                 #
# --------------------------------------------------------------------------- #


def _csrf_serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt=_CSRF_SALT)


def make_csrf_token(secret: str, now: datetime) -> str:
    """Mint a signed CSRF token (bound to the issuing instant)."""
    return _csrf_serializer(secret).dumps({"issued_at": now.isoformat()})


def validate_csrf(secret: str, token: str) -> bool:
    """True iff ``token`` carries a valid signature under the CSRF salt (an attacker can't
    forge one without the secret). Forged / tampered / empty -> False."""
    if not token:
        return False
    try:
        data = _csrf_serializer(secret).loads(token)
    except BadSignature:
        return False
    return isinstance(data, dict) and "issued_at" in data


# --------------------------------------------------------------------------- #
# Login throttle / lockout (in-memory, per user+ip)                           #
# --------------------------------------------------------------------------- #


class LoginThrottle:
    """Tracks recent failed logins per key and locks out after too many within a window.

    In-memory / process-local (single admin); resets on restart. Pass ``now`` explicitly
    so lockout timing is deterministic in tests."""

    def __init__(self, max_attempts: int, lockout_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._failures: dict[str, list[datetime]] = {}

    @staticmethod
    def key(username: str, client_ip: str) -> str:
        return f"{username}\x00{client_ip}"

    def _recent(self, key: str, now: datetime) -> list[datetime]:
        cutoff = self._lockout_seconds
        recent = [t for t in self._failures.get(key, []) if (now - t).total_seconds() <= cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def record_failure(self, key: str, now: datetime) -> None:
        self._failures.setdefault(key, []).append(now)

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)

    def is_locked(self, key: str, now: datetime) -> bool:
        return len(self._recent(key, now)) >= self._max_attempts


__all__ = [
    "LoginThrottle",
    "make_csrf_token",
    "make_session_token",
    "read_session_token",
    "validate_csrf",
    "verify_password",
]
