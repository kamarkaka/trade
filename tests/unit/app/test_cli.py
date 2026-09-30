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


def test_reconcile_in_paper_mode_has_nothing_to_do() -> None:
    result = runner.invoke(app, ["reconcile"])  # config/default.yaml is paper
    assert result.exit_code == 0
    assert "paper mode" in result.output


def test_reconcile_refuses_while_another_process_holds_the_lease(tmp_path: Path) -> None:
    from trader.state.lease import TradingLease

    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "live", tmp_path)
    with TradingLease(tmp_path / "state.sqlite"):  # e.g. the running daemon
        result = runner.invoke(app, ["reconcile", "--config", str(cfg)])
    assert result.exit_code == 3 and "trading lease" in result.output


def test_reconcile_operator_overrides_settle_rows_before_touching_the_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.core import Order
    from trader.core.enums import OrderType, Side
    from trader.execution.idempotency import OrderRepository
    from trader.state.db import connect
    from trader.state.migrate import run_migrations

    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)  # stop right after the overrides
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    repo = OrderRepository(conn)
    for cid in ("c-gone", "c-found"):
        repo._write_pending(Order(cid, "s1", "AAPL", Side.BUY, 1, OrderType.MARKET))
        repo.mark_unknown_after_send(cid)
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "live", tmp_path)
    result = runner.invoke(
        app,
        [
            "reconcile",
            "--config",
            str(cfg),
            "--mark-not-placed",
            "c-gone",
            "--adopt",
            "c-found=SCH-7",
        ],
    )
    assert "override: c-gone marked not placed" in result.output
    assert "override: c-found adopted as SCH-7" in result.output
    gone, found = repo.get("c-gone"), repo.get("c-found")
    assert gone is not None and gone.status == "not_placed"
    assert found is not None and (found.status, found.broker_order_id) == ("WORKING", "SCH-7")
    assert result.exit_code == 1  # then stops: no credentials to reach the broker


def test_reconcile_rejects_a_malformed_override(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "live", tmp_path)
    result = runner.invoke(app, ["reconcile", "--config", str(cfg), "--adopt", "no-equals"])
    assert result.exit_code == 2 and "CID=BROKER_ORDER_ID" in result.output


def test_run_refuses_while_another_process_holds_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.state.lease import TradingLease

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "paper", tmp_path)
    with TradingLease(tmp_path / "state.sqlite"):
        result = runner.invoke(app, ["run", "--config", str(cfg), "--once"])
    assert result.exit_code == 3 and "trading lease" in result.output


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


def test_run_refuses_live_without_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADER_CONFIRM_LIVE", raising=False)
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "live", tmp_path)
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "SECOND confirmation" in result.output  # double-confirm gate (M5.6)


def test_run_rejects_backtest_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write_run_config(cfg, "backtest", tmp_path)
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "mode=paper or mode=live" in result.output


def test_no_real_order_path_until_confirmed_live() -> None:
    # CI tripwire (design safety gate), updated for M5.6: live REQUIRES a second confirmation
    # (test_run_refuses_live_without_confirm) so real orders can never start silently; paper
    # uses SimBroker. The READ-ONLY Schwab client still exposes no order/cancel path (writes
    # live on the separate SchwabTradingClient).
    import inspect

    from trader.app import cli
    from trader.schwab.endpoints import SchwabClient

    order_methods = [
        m for m in dir(SchwabClient) if any(k in m for k in ("submit", "order", "cancel"))
    ]
    assert order_methods == [], f"unexpected order path on read-only SchwabClient: {order_methods}"
    # SchwabBroker is only constructed inside the live branch, which is unreachable without the
    # double-confirm gate; paper still uses SimBroker.
    run_src = inspect.getsource(cli.run)
    assert "live_confirmed(" in run_src  # the double-confirm gate is present
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


def test_cli_installs_the_scrubbed_logging_pipeline() -> None:
    # Every command runs behind the app callback, which must replace structlog's defaults
    # (tracebacks with frame locals, no secret scrubbing) with our pipeline.
    import structlog

    from trader.observability.logging import _scrub_processor

    structlog.reset_defaults()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    processors = structlog.get_config()["processors"]
    assert _scrub_processor in processors
    assert structlog.processors.format_exc_info in processors
