"""Cross-cutting credential-leakage guard (M1.10 exit gate, design §13).

Runs representative client flows (token exchange, refresh, quotes, account
numbers) with logging routed to a buffer, then asserts that no access/refresh
token, app secret, auth code, or raw account number ever appears in any log
output or in a raised exception's message. Also pins the central scrub behavior
(key-based + literal-based) and the masking reprs.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.oauth import exchange_code, refresh_access_token
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.observability.logging import (
    REDACTED,
    clear_secrets,
    configure_logging,
    get_logger,
)
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import ACCOUNT_NUMBERS_PATH, OAUTH_TOKEN_URL, QUOTES_PATH
from trader.schwab.endpoints import SchwabClient
from trader.schwab.errors import SchwabBadResponseError, SchwabRefreshTokenDeadError
from trader.schwab.http import SchwabHttp
from trader.schwab.models import AccountNumberMapping

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)

# Distinctive, long literals so a substring match is unambiguous (no accidental
# over-redaction of unrelated text).
ACCESS = "ATSECRET_aaaaaaaaaaaaaaaaaaaa"
REFRESH = "RTSECRET_bbbbbbbbbbbbbbbbbbbb"
CODE = "AUTHCODE_cccccccccccccccccccc"
APP_SECRET = "APPSECRET_dddddddddddddddddddd"
ACCOUNT = "ACCT_9876543210"
HASH = "HASHVALUE_eeeeeeeeeeee"

ALL_SECRETS = (ACCESS, REFRESH, CODE, APP_SECRET, ACCOUNT)


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    # Registered literals are process-global; isolate this module's tests.
    clear_secrets()
    yield
    clear_secrets()


def _cfg(tmp_path: Path) -> SchwabClientConfig:
    return SchwabClientConfig(
        app_key="APPKEY",
        app_secret=APP_SECRET,  # pydantic coerces to SecretStr
        token_store_path=tmp_path / "t.sqlite",
    )


def _quote_response() -> dict[str, object]:
    return {
        "AAPL": {
            "quote": {
                "lastPrice": 150.25,
                "bidPrice": 150.2,
                "askPrice": 150.3,
                "totalVolume": 1000,
                "quoteTime": 1782556800000,
                "closePrice": 149.5,
            }
        }
    }


@respx.mock
def test_no_token_in_logs(tmp_path: Path) -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", json_output=True, stream=buf)
    cfg = _cfg(tmp_path)
    clock = FakeClock(NOW)

    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 1800}
        )
    )
    respx.get(QUOTES_PATH).mock(return_value=httpx.Response(200, json=_quote_response()))
    respx.get(ACCOUNT_NUMBERS_PATH).mock(
        return_value=httpx.Response(200, json=[{"accountNumber": ACCOUNT, "hashValue": HASH}])
    )

    with httpx.Client() as client:
        # token flows register ACCESS/REFRESH/CODE as scrub literals
        exchange_code(client, cfg, CODE, clock)
        refresh_access_token(client, cfg, REFRESH, clock, issued_at=NOW)

        store = TokenStore(cfg.token_store_path)
        store.save(TokenSet(ACCESS, REFRESH, NOW + timedelta(seconds=1800), NOW))
        sc = SchwabClient(SchwabHttp(cfg, client, store, clock=FakeClock(NOW)))
        sc.get_quotes(["AAPL"])
        sc.get_account_numbers()

    # Adversarially try to leak every secret through the log pipeline.
    log = get_logger("leak.test")
    log.info(
        f"attempt access={ACCESS} refresh={REFRESH} code={CODE} acct={ACCOUNT}",
        access_token=ACCESS,
        refresh_token=REFRESH,
        app_secret=APP_SECRET,
        authorization=f"Bearer {ACCESS}",
        account_number=ACCOUNT,
        nested={"deep": [REFRESH, {"x": CODE}]},
    )

    out = buf.getvalue()
    for secret in ALL_SECRETS:
        assert secret not in out, f"{secret!r} leaked into logs"
    assert REDACTED in out  # scrubbing actually fired


@respx.mock
def test_no_secret_in_exception_messages(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    clock = FakeClock(NOW)

    # dead refresh token -> SchwabRefreshTokenDeadError
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with httpx.Client() as client, pytest.raises(SchwabRefreshTokenDeadError) as ei:
        refresh_access_token(client, cfg, REFRESH, clock, issued_at=NOW)
    assert REFRESH not in str(ei.value)

    # malformed success body (no access_token) -> SchwabBadResponseError
    respx.post(OAUTH_TOKEN_URL).mock(return_value=httpx.Response(200, json={"nope": 1}))
    with httpx.Client() as client, pytest.raises(SchwabBadResponseError) as ei2:
        exchange_code(client, cfg, CODE, clock)
    assert CODE not in str(ei2.value)


def test_no_raw_account_number_leak() -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", json_output=True, stream=buf)
    mapping = AccountNumberMapping(account_number=ACCOUNT, hash_value=HASH)

    # repr of the object must not expose the raw number
    assert ACCOUNT not in repr(mapping)
    assert HASH in repr(mapping)

    # logging the raw number under the sensitive key redacts it; hash is fine
    log = get_logger("acct.test")
    log.info("resolved account", account_number=mapping.account_number, hash=mapping.hash_value)
    out = buf.getvalue()
    assert ACCOUNT not in out
    assert HASH in out  # hashed id is safe to log


def test_authorization_header_value_scrubbed() -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", json_output=True, stream=buf)
    log = get_logger("hdr.test")
    log.info("call", authorization="Basic QVBQS0VZOlNFQ1JFVA==")
    out = buf.getvalue()
    assert "QVBQS0VZOlNFQ1JFVA==" not in out
    assert REDACTED in out
