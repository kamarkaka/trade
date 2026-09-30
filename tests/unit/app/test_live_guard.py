"""Go-live guard: double-confirm refusal, conservative preflight (effective per-strategy
caps, alert channel, idempotency blocker), and the startup-live alert (M5.6). CI-enforced
so the real-money gate is never manual."""

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
from trader.core.types import StrategyBinding

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


def _bindings(*, overrides: dict[str, object] | None = None) -> list[StrategyBinding]:
    return [
        StrategyBinding(
            strategy_id="m",
            strategy_name="threshold",
            params={},
            universe=("AAPL",),
            slots=(),
            enabled=True,
            risk_overrides=overrides,
        )
    ]


# safe account config that clears every NON-idempotency check
def _safe_cfg() -> AppConfig:
    return _cfg(
        allowlist=["AAPL"],
        max_order_notional_usd=Decimal("500"),
        max_position_size_pct=2.0,
        max_gross_exposure_usd=Decimal("4000"),
    )


def _pf(config: AppConfig, bindings: list[StrategyBinding], **kw: object) -> list[PreflightProblem]:
    defaults: dict[str, object] = {
        "kill_switch_engaged": False,
        "token_valid": True,
        "alert_channel_count": 1,
    }
    defaults.update(kw)
    return live_preflight(config, bindings, **defaults)  # type: ignore[arg-type]


# --- double confirm --------------------------------------------------------- #


def test_live_confirmed_signals() -> None:
    assert live_confirmed(confirm_flag=False, environ={}) is False
    assert live_confirmed(confirm_flag=True, environ={}) is True
    assert live_confirmed(confirm_flag=False, environ={CONFIRM_ENV_VAR: "I_UNDERSTAND"}) is True
    assert live_confirmed(confirm_flag=False, environ={CONFIRM_ENV_VAR: "yes"}) is False
    assert live_confirmed(confirm_flag=False, environ={CONFIRM_ENV_VAR: ""}) is False


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


# --- preflight: idempotency blocker (M5.6 refuses live until M5.7) ----------- #


def test_preflight_refuses_until_idempotent_by_default() -> None:
    # A fully clean config still cannot go live in M5.6: the submit path isn't idempotent yet.
    problems = _pf(_safe_cfg(), _bindings())
    assert [p.check for p in problems] == ["idempotency"]


def test_preflight_clean_passes_when_order_path_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trader.app.live_guard.LIVE_ORDER_PATH_READY", True)  # simulate M5.7
    assert _pf(_safe_cfg(), _bindings()) == []


# --- preflight: conservative checks (with the blocker simulated off) --------- #


def test_preflight_requires_allowlist_and_small_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trader.app.live_guard.LIVE_ORDER_PATH_READY", True)
    # default RiskConfig: no allowlist, notional 5000 > 1000, position 10% > 5%, gross 25k > 5k
    checks = {p.check for p in _pf(_cfg(), _bindings())}
    assert {
        "allowlist",
        "max_order_notional_usd",
        "max_position_size_pct",
        "max_gross_exposure_usd",
    } <= checks


def test_preflight_blocks_per_strategy_override_exceeding_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The key bypass: account caps are tiny, but a strategy raises notional above the ceiling.
    monkeypatch.setattr("trader.app.live_guard.LIVE_ORDER_PATH_READY", True)
    cfg = _safe_cfg()
    bindings = _bindings(overrides={"max_order_notional_usd": 50000})
    problems = _pf(cfg, bindings)
    assert any(p.check == "max_order_notional_usd" and "m" in p.detail for p in problems)


def test_preflight_requires_alert_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trader.app.live_guard.LIVE_ORDER_PATH_READY", True)
    problems = _pf(_safe_cfg(), _bindings(), alert_channel_count=0)
    assert any(p.check == "alerting" for p in problems)


def test_preflight_blocks_on_kill_switch_token_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trader.app.live_guard.LIVE_ORDER_PATH_READY", True)
    assert any(
        p.check == "kill_switch" for p in _pf(_safe_cfg(), _bindings(), kill_switch_engaged=True)
    )
    assert any(p.check == "token" for p in _pf(_safe_cfg(), _bindings(), token_valid=False))


# --- the startup reconciliation gate (LR10) ---------------------------------------- #


def _report(*, clean: bool, retry_later: bool = False):  # type: ignore[no-untyped-def]
    from trader.execution.account_reconcile import (
        AccountReconcileReport,
        OrderSettlement,
        Settlement,
    )
    from trader.execution.reconcile import ReconcileReport

    code = "window_open" if retry_later else "ambiguous"
    orders = () if clean else (OrderSettlement("c1", Settlement.UNRESOLVED, "x", code),)
    return AccountReconcileReport(orders, ReconcileReport())


def test_a_clean_account_passes_the_gate_at_once() -> None:
    from trader.app.live_guard import reconcile_before_live

    calls, waits = [], []
    report = reconcile_before_live(
        lambda: calls.append(1) or _report(clean=True), wait=waits.append, window_seconds=300
    )
    assert report.is_clean and calls == [1] and waits == []


def test_a_pending_window_is_waited_out_once() -> None:
    from trader.app.live_guard import reconcile_before_live

    reports = iter([_report(clean=False, retry_later=True), _report(clean=True)])
    waits: list[float] = []
    report = reconcile_before_live(lambda: next(reports), wait=waits.append, window_seconds=300)
    assert report.is_clean and waits == [300]


def test_an_account_needing_attention_is_not_retried() -> None:
    from trader.app.live_guard import reconcile_before_live

    calls, waits = [], []
    report = reconcile_before_live(
        lambda: calls.append(1) or _report(clean=False), wait=waits.append, window_seconds=300
    )
    assert not report.is_clean and calls == [1] and waits == []


def test_still_unclean_after_the_wait_is_returned_unclean() -> None:
    from trader.app.live_guard import reconcile_before_live

    waits: list[float] = []
    report = reconcile_before_live(
        lambda: _report(clean=False, retry_later=True), wait=waits.append, window_seconds=300
    )
    assert not report.is_clean and waits == [300]  # waits once, never loops


# --- startup alert ---------------------------------------------------------- #


def test_startup_alert_on_live() -> None:
    events: list[AlertEvent] = []

    class _Rec:
        def alert(self, event: AlertEvent) -> None:
            events.append(event)

    announce_live(_Rec())
    assert len(events) == 1
    assert "LIVE" in events[0].message and events[0].severity.value == "CRITICAL"
