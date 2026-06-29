"""Tests for the OAuth loopback callback server (parsing + cert + TLS roundtrip)."""

import ssl
import stat
import threading
from pathlib import Path

import httpx
import pytest

from trader.auth.callback_server import (
    CallbackServer,
    generate_self_signed_cert,
    parse_callback_query,
)
from trader.schwab.errors import SchwabAuthError


def test_parses_code_and_state() -> None:
    result = parse_callback_query("/?code=abc&state=xyz")
    assert result.code == "abc"
    assert result.state == "xyz"


def test_code_without_state() -> None:
    assert parse_callback_query("/?code=abc").state is None


def test_error_callback_raises() -> None:
    with pytest.raises(SchwabAuthError):
        parse_callback_query("/?error=access_denied")


def test_missing_code_raises() -> None:
    with pytest.raises(SchwabAuthError):
        parse_callback_query("/?state=xyz")


def test_binds_loopback_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        CallbackServer(tmp_path / "c.pem", tmp_path / "k.pem", host="0.0.0.0")


def test_generate_self_signed_cert_is_usable(tmp_path: Path) -> None:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert, key)
    # Key has restrictive perms.
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # Loads into a real SSLContext without error.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))


def test_wait_for_code_timeout_cleans_up(tmp_path: Path) -> None:
    # Offline lifecycle coverage: bind a loopback TLS socket, never connect, time
    # out, and confirm cleanup frees the socket (re-bind succeeds).
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert, key)
    server = CallbackServer(cert, key, port=0)
    server.start()
    with pytest.raises(SchwabAuthError):
        server.wait_for_code(timeout=0.05)
    server2 = CallbackServer(cert, key, port=0)
    server2.start()
    server2.stop()  # idempotent-safe cleanup


@pytest.mark.network
def test_https_roundtrip_captures_code(tmp_path: Path) -> None:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert, key)
    server = CallbackServer(cert, key, port=0, expected_state="st")
    server.start()
    captured: dict[str, object] = {}

    def _wait() -> None:
        captured["result"] = server.wait_for_code(timeout=5)

    waiter = threading.Thread(target=_wait)
    waiter.start()
    resp = httpx.get(f"https://127.0.0.1:{server.bound_port}/?code=THECODE&state=st", verify=False)
    assert resp.status_code == 200
    waiter.join(timeout=5)
    result = captured["result"]
    assert result.code == "THECODE"


@pytest.mark.network
def test_https_roundtrip_state_mismatch_errors(tmp_path: Path) -> None:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    generate_self_signed_cert(cert, key)
    server = CallbackServer(cert, key, port=0, expected_state="expected")
    server.start()
    err: dict[str, BaseException] = {}

    def _wait() -> None:
        try:
            server.wait_for_code(timeout=5)
        except BaseException as exc:
            err["e"] = exc

    waiter = threading.Thread(target=_wait)
    waiter.start()
    httpx.get(f"https://127.0.0.1:{server.bound_port}/?code=C&state=WRONG", verify=False)
    waiter.join(timeout=5)
    assert isinstance(err.get("e"), SchwabAuthError)
