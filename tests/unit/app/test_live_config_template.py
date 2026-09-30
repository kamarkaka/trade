"""The committed live config template (LR13) must stay a safe starting point: it loads,
passes the guarded-rollout preflight except for the deliberately-off LIVE_ORDER_PATH_READY
flag, and trades one long-only canary within its allowlist."""

from pathlib import Path

import pytest

from trader.app.live_guard import live_preflight
from trader.config import load_config
from trader.core.enums import Mode
from trader.strategy import load_bindings

TEMPLATE = Path(__file__).parents[3] / "config" / "live.example.yaml"


def _preflight(**overrides: object):  # type: ignore[no-untyped-def]
    cfg = load_config(TEMPLATE, environ={})
    _, bindings = load_bindings(cfg)
    inputs: dict[str, object] = {
        "kill_switch_engaged": False,
        "token_valid": True,
        "alert_channel_count": len(cfg.alerting.channels),
        **overrides,
    }
    return cfg, bindings, live_preflight(cfg, bindings, **inputs)  # type: ignore[arg-type]


def test_template_is_live_and_only_blocked_by_the_order_path_flag() -> None:
    cfg, _, problems = _preflight()
    assert cfg.mode is Mode.LIVE
    assert [p.check for p in problems] == ["idempotency"]  # LIVE_ORDER_PATH_READY is False


def test_template_passes_preflight_once_the_order_path_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("trader.app.live_guard.LIVE_ORDER_PATH_READY", True)
    _, _, problems = _preflight()
    assert problems == []


def test_template_trades_one_long_only_canary_inside_its_allowlist() -> None:
    cfg, bindings, _ = _preflight()
    enabled = [b for b in bindings if b.enabled]
    assert [(b.strategy_name, b.params) for b in enabled] == [("canary", {"lot": 1})]
    assert all(len(b.slots) == 1 for b in enabled)  # one slot per session: no day-trades
    universe = {s for b in enabled for s in b.universe}
    assert universe and universe <= set(cfg.risk.allowlist)


def test_live_state_never_shares_the_paper_database() -> None:
    live = load_config(TEMPLATE, environ={})
    paper = load_config(TEMPLATE.parent / "default.yaml", environ={})
    assert live.observability.db_path != paper.observability.db_path
    assert live.observability.db_path.startswith("/state/")  # on the durable volume
