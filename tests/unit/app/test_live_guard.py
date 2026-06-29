"""Go-live guard: double-confirm refusal, conservative preflight, and the mandatory
startup-live alert (M5.6). CI-enforced so the real-money gate is never manual."""

from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.app.cli import app
from trader.app.live_guard import (
    CONFIRM_ENV_VAR,
    AlertEvent,
    PreflightProblem,
    announce_live,
    live_confirmed,
    live_preflight,
)
from trader.config.models import AppConfig

runner = CliRunner()


def _cfg(**risk: object) -> AppConfig:
    base = {
        "mode": "live",
        "strategies": [
            {
                "id": "m",
                "name": "threshold",
                "universe": ["AAPL"],
                "slots": [{"id": "o", "time": "09:45"}],
            }
        ],
        "risk": risk,
    }
    return AppConfig.model_validate(base)


# --- double confirm --------------------------------------------------------- #


def test_live_confirmed_signals() -> None:
    assert live_confirmed(confirm_flag=False, environ={}) is False
    assert live_confirmed(confirm_flag=True, environ={}) is True
    assert live_confirmed(confirm_flag=False, environ={CONFIRM_ENV_VAR: "I_UNDERSTAND"}) is True
    assert (
        live_confirmed(confirm_flag=False, environ={CONFIRM_ENV_VAR: "yes"}) is False
    )  # wrong phrase


def test_refuses_live_without_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIRM_ENV_VAR, raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "mode: live\nstrategies:\n  - id: m\n    name: threshold\n    universe: [AAPL]\n"
        '    slots:\n      - {id: o, time: "09:45"}\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "SECOND confirmation" in result.output  # exits at the gate, before any network


# --- preflight -------------------------------------------------------------- #


def test_preflight_requires_allowlist_and_small_caps() -> None:
    # Default RiskConfig: no allowlist, notional 5000 > 1000, position 10% > 5%.
    problems = live_preflight(_cfg(), kill_switch_engaged=False, token_valid=True)
    checks = {p.check for p in problems}
    assert "allowlist" in checks
    assert "max_order_notional_usd" in checks
    assert "max_position_size_pct" in checks


def test_preflight_clean_passes() -> None:
    problems = live_preflight(
        _cfg(
            allowlist=["AAPL"],
            max_order_notional_usd=Decimal("500"),
            max_position_size_pct=2.0,
        ),
        kill_switch_engaged=False,
        token_valid=True,
    )
    assert problems == []


def test_preflight_blocks_on_kill_switch_and_token() -> None:
    cfg = _cfg(allowlist=["AAPL"], max_order_notional_usd=Decimal("500"), max_position_size_pct=2.0)
    ks = live_preflight(cfg, kill_switch_engaged=True, token_valid=True)
    assert any(p.check == "kill_switch" for p in ks)
    tok = live_preflight(cfg, kill_switch_engaged=False, token_valid=False)
    assert any(p.check == "token" for p in tok)
    recon = live_preflight(cfg, kill_switch_engaged=False, token_valid=True, reconcile_clean=False)
    assert any(p.check == "reconcile" for p in recon)


# --- startup alert ---------------------------------------------------------- #


def test_startup_alert_on_live() -> None:
    events: list[AlertEvent] = []

    class _Rec:
        def alert(self, event: AlertEvent) -> None:
            events.append(event)

    announce_live(_Rec())
    assert len(events) == 1
    assert "LIVE" in events[0].message and events[0].severity.value == "CRITICAL"


def test_preflight_problem_is_structured() -> None:
    p = PreflightProblem("x", "y")
    assert (p.check, p.detail) == ("x", "y")
