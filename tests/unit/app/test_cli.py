"""Tests for the Typer CLI skeleton: command listing, status output, healthcheck
exit code, config-error handling, and stub commands."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.app.cli import app
from trader.config import DEFAULT_CONFIG_PATH

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "backtest", "status", "reauth", "kill", "reconcile"):
        assert cmd in result.output


def test_status_prints_mode_and_strategies() -> None:
    result = runner.invoke(app, ["status", "--config", str(DEFAULT_CONFIG_PATH)])
    assert result.exit_code == 0
    assert "mode: paper" in result.output
    assert "momentum" in result.output  # from config/default.yaml
    assert "not authenticated" in result.output


def test_status_uses_default_config_when_omitted() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "mode: paper" in result.output


def test_healthcheck_exits_zero_when_config_loads() -> None:
    result = runner.invoke(app, ["status", "--healthcheck"])
    assert result.exit_code == 0


def test_status_invalid_config_exits_nonzero(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("strategies: []\n", encoding="utf-8")  # violates min_length=1
    result = runner.invoke(app, ["status", "--config", str(bad)])
    assert result.exit_code != 0
    assert "config error" in result.output


def test_healthcheck_invalid_config_exits_nonzero(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("strategies: []\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--healthcheck", "--config", str(bad)])
    assert result.exit_code != 0


def test_stub_commands_run() -> None:
    for argv in (["run"], ["backtest"], ["reconcile"], ["kill", "--on"]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, argv
        assert "not implemented" in result.output


def test_status_reports_token_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from trader.auth.token_store import TokenStore
    from trader.auth.tokens import TokenSet

    tok_path = tmp_path / "tok.sqlite"
    monkeypatch.setenv("SCHWAB_TOKEN_STORE_PATH", str(tok_path))
    now = datetime.now(UTC)
    TokenStore(tok_path).save(
        TokenSet("ACC", "REF", now + timedelta(hours=1), now - timedelta(days=1))
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "auth: authenticated" in result.output
    assert "expires in" in result.output


def test_reauth_without_credentials_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    result = runner.invoke(app, ["reauth"])
    assert result.exit_code != 0
    assert "reauth error" in result.output


def test_data_fetch_without_credentials_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    result = runner.invoke(
        app, ["data", "fetch", "--symbols", "AAPL", "--start", "2023-01-01", "--end", "2023-12-31"]
    )
    assert result.exit_code != 0
    assert "data fetch error" in result.output


def test_data_fetch_rejects_bad_date(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(
        app, ["data", "fetch", "--symbols", "AAPL", "--start", "01/01/2023", "--end", "2023-12-31"]
    )
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output


def test_data_fetch_rejects_non_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(
        app,
        [
            "data",
            "fetch",
            "--symbols",
            "AAPL",
            "--start",
            "2023-01-01",
            "--end",
            "2023-12-31",
            "--freq",
            "minute",
        ],
    )
    assert result.exit_code != 0
    assert "only --freq daily" in result.output


def test_console_script_entrypoint_is_callable() -> None:
    assert callable(app)  # Typer instances are callable (console_scripts entry point)
