"""Tests for the Typer CLI skeleton: command listing, status output, healthcheck
exit code, config-error handling, and stub commands."""

from pathlib import Path

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
    for argv in (["run"], ["backtest"], ["reconcile"], ["reauth"], ["kill", "--on"]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, argv
        assert "not implemented" in result.output


def test_console_script_entrypoint_is_callable() -> None:
    assert callable(app)  # Typer instances are callable (console_scripts entry point)
