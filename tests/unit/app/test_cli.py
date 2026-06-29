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


def test_healthcheck_nonzero_without_heartbeat() -> None:
    # No running daemon => no fresh heartbeat at the configured db_path => unhealthy.
    # (Fresh/stale exit codes are covered in test_heartbeat.py with a real state DB.)
    result = runner.invoke(app, ["status", "--healthcheck"])
    assert result.exit_code != 0


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
    # `kill` is implemented as of M5.4 (see test_kill_switch.py); `reconcile` is still a stub.
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0
    assert "not implemented" in result.output


def _write_run_config(path: Path, mode: str, data_cache: Path) -> None:
    path.write_text(
        f"""
mode: {mode}
strategies:
  - id: momentum
    name: threshold
    universe: [AAPL]
    slots:
      - {{id: open, time: "09:45"}}
observability:
  data_cache: "{data_cache}"
  db_path: "{data_cache / "state.sqlite"}"
""",
        encoding="utf-8",
    )


def test_run_refuses_live_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "live", tmp_path)
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "live mode is refused" in result.output


def test_run_requires_paper_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "backtest", tmp_path)
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "requires mode=paper" in result.output


def test_no_real_order_path_until_go_live() -> None:
    # CI tripwire (design safety gate), updated for M5: SchwabBroker now EXISTS (M5.2) but is
    # not wired into the daemon -- the paper `run` path constructs SimBroker only and refuses
    # mode=live. Real orders are only possible via the go-live double-confirm (M5.6) + manual
    # verification (M5.7). The READ-ONLY Schwab client still exposes no order/cancel path
    # (writes live on the separate SchwabTradingClient).
    import inspect

    from trader.app import cli
    from trader.schwab.endpoints import SchwabClient

    order_methods = [
        m for m in dir(SchwabClient) if any(k in m for k in ("submit", "order", "cancel"))
    ]
    assert order_methods == [], f"unexpected order path on read-only SchwabClient: {order_methods}"
    # The `run` command must not CONSTRUCT the live SchwabBroker (paper uses SimBroker);
    # mode=live is refused in depth by test_run_refuses_live_mode. (Check construction, not
    # mere mentions in comments.)
    run_src = inspect.getsource(cli.run)
    assert "SchwabBroker(" not in run_src, "run must not wire the live broker until go-live (M5.6)"
    assert "SimBroker(" in run_src  # paper path still uses the simulator


def test_run_paper_without_credentials_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "paper", tmp_path)
    result = runner.invoke(app, ["run", "--config", str(cfg), "--once"])
    assert result.exit_code != 0
    assert "run error" in result.output  # mode ok, fails at credential resolution


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


def test_data_fetch_rejects_reversed_range(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(
        app, ["data", "fetch", "--symbols", "AAPL", "--start", "2023-12-31", "--end", "2023-01-01"]
    )
    assert result.exit_code != 0
    assert "on or after" in result.output


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
