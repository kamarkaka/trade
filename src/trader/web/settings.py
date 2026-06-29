"""Web UI settings (design §19, M7.2).

``WebSettings`` is a frozen pydantic model holding everything the read-only monitoring
service needs: the single admin's username + argon2id password hash, the session-cookie
signing secret, the read-only state DB path, and session / login-lockout / auto-refresh
tunables. Loaded from the environment (the compose ``env_file``) via ``from_env`` — kept as a
plain model + explicit env reader (no pydantic-settings dependency), consistent with the
project's manual config approach. NOTHING here is written back; the web service is read-only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr


class WebSettings(BaseModel):
    """Read-only monitoring-UI configuration (injected into the app factory)."""

    model_config = ConfigDict(frozen=True)

    admin_user: str
    # SecretStr so the hash + signing key never appear in a repr/str/log/traceback-locals
    # dump (matches the M1 token convention). Read at the verify/sign call site via
    # ``.get_secret_value()``.
    admin_password_hash: SecretStr  # argon2id hash; NEVER the plaintext password
    session_secret: SecretStr  # signs the stateless session cookie (itsdangerous)
    db_path: Path  # read-only handle onto the trading state DB (observability.db_path)
    config_path: Path = Path("/config/config.yaml")
    session_idle_seconds: int = 1800  # 30 min idle timeout
    session_absolute_seconds: int = 28800  # 8 h absolute cap
    login_max_attempts: int = 5  # before lockout
    login_lockout_seconds: int = 300  # 5 min lockout window
    auto_refresh_seconds: int = 15  # HTMX dashboard poll interval
    cookie_secure: bool = True  # Secure flag on the session cookie (TLS-only); False for http tests

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WebSettings:
        """Build settings from environment variables (compose ``env_file``). Raises
        ``ValueError`` if a required secret/credential is missing."""
        env = os.environ if environ is None else environ

        def _required(key: str) -> str:
            value = env.get(key)
            if not value:
                raise ValueError(f"missing required web env var: {key}")
            return value

        def _int(key: str, default: int) -> int:
            raw = env.get(key)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"invalid integer for web env var {key}: {raw!r}") from exc

        return cls(
            admin_user=_required("WEB_ADMIN_USER"),
            admin_password_hash=SecretStr(_required("WEB_ADMIN_PASSWORD_HASH")),
            session_secret=SecretStr(_required("SESSION_SECRET")),
            db_path=Path(env.get("WEB_DB_PATH", "/state/trader.sqlite")),
            config_path=Path(env.get("WEB_CONFIG_PATH", "/config/config.yaml")),
            session_idle_seconds=_int("SESSION_IDLE_SECONDS", 1800),
            session_absolute_seconds=_int("SESSION_ABSOLUTE_SECONDS", 28800),
            login_max_attempts=_int("LOGIN_MAX_ATTEMPTS", 5),
            login_lockout_seconds=_int("LOGIN_LOCKOUT_SECONDS", 300),
            auto_refresh_seconds=_int("AUTO_REFRESH_SECONDS", 15),
            cookie_secure=env.get("WEB_COOKIE_SECURE", "true").lower() not in ("0", "false", "no"),
        )


__all__ = ["WebSettings"]
