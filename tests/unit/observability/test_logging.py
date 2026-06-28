"""Tests for structured logging: JSON shape, secret scrubbing (keys, nested
headers, account number, registered literals), correlation id, and level filter."""

import io
import json
import logging
from typing import Any

import pytest
import structlog

from trader.observability.logging import (
    bind_cycle_id,
    clear_cycle_id,
    clear_secrets,
    configure_logging,
    cycle_context,
    get_logger,
    register_secret,
)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    structlog.contextvars.clear_contextvars()
    clear_secrets()
    yield
    structlog.contextvars.clear_contextvars()
    clear_secrets()


def _one(buf: io.StringIO) -> dict[str, Any]:
    return json.loads(buf.getvalue().strip())


def test_emits_json() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger().info("hello", foo="bar")
    d = _one(buf)
    assert d["event"] == "hello"
    assert d["foo"] == "bar"
    assert d["level"] == "info"
    assert "timestamp" in d


def test_scrubs_token_key() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger().info("auth", access_token="SEKRIT")
    out = buf.getvalue()
    assert "SEKRIT" not in out
    assert _one(buf)["access_token"] == "***"


def test_scrubs_nested_authorization_header() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger().info("request", headers={"Authorization": "Bearer abc.def.ghi"})
    out = buf.getvalue()
    assert "abc.def.ghi" not in out
    assert _one(buf)["headers"]["Authorization"] == "***"


def test_scrubs_raw_account_number() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger().info("acct", account_number="123456789")
    assert "123456789" not in buf.getvalue()


def test_registered_secret_literal_scrubbed_anywhere() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    register_secret("supersecretvalue")
    get_logger().info("token supersecretvalue inline", detail="x supersecretvalue y")
    out = buf.getvalue()
    assert "supersecretvalue" not in out
    assert "***" in out


def test_cycle_id_via_context_manager() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    with cycle_context("cyc-1"):
        get_logger().info("x")
    assert _one(buf)["cycle_id"] == "cyc-1"


def test_bind_cycle_id_generates_when_none() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    cid = bind_cycle_id()
    try:
        get_logger().info("x")
        assert _one(buf)["cycle_id"] == cid
        assert len(cid) > 0
    finally:
        clear_cycle_id()


def test_level_filtering() -> None:
    buf = io.StringIO()
    configure_logging(level="WARNING", stream=buf)
    get_logger().info("below-threshold")
    assert buf.getvalue() == ""  # filtered out
    get_logger().warning("warnmsg")
    assert "warnmsg" in buf.getvalue()


def test_scrubs_inside_lists() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger().info("orders", items=[{"access_token": "X"}, {"ok": 1}])
    out = buf.getvalue()
    assert "X" not in out
    d = _one(buf)
    assert d["items"][0]["access_token"] == "***"
    assert d["items"][1]["ok"] == 1


def test_accepts_int_level_and_passes_non_strings() -> None:
    buf = io.StringIO()
    configure_logging(level=logging.INFO, stream=buf)
    get_logger().info("nums", count=5, ratio=1.5)
    d = _one(buf)
    assert d["count"] == 5
    assert d["ratio"] == 1.5


def test_register_secret_ignores_empty() -> None:
    register_secret(None)
    register_secret("")
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger().info("plain message")
    assert "plain message" in buf.getvalue()
