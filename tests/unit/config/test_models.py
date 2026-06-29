"""Tests for the configuration schema: parse the §11 example and exercise the
validation rules (drift ceiling, unique ids, override-key subset, enums, tz,
time format, extra-key rejection, positivity, enabled-strategy requirements)."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from trader.config import AppConfig
from trader.core.enums import ConflictPolicy, Mode, OrderType

# A full, valid configuration mirroring design §11.
EXAMPLE: dict[str, Any] = {
    "mode": "paper",
    "account": {"broker": "schwab", "account_ref": "primary", "secrets_ref": "keychain"},
    "schedule": {
        "timezone": "America/New_York",
        "market_calendar": "XNYS",
        "base_seed": None,
        "catch_up": False,
        "misfire_grace_seconds": 120,
    },
    "strategies": [
        {
            "id": "momentum",
            "name": "threshold",
            "enabled": True,
            "params": {"band": 0.02, "lot": 10},
            "universe": ["AAPL", "MSFT"],
            "slots": [
                {
                    "id": "morning",
                    "time": "09:45",
                    "drift_max_minutes": 30,
                    "drift_direction": "forward",
                    "distribution": "uniform",
                    "on_overshoot": "clamp",
                },
                {"id": "midday", "time": "12:30", "drift_max_minutes": 30},
            ],
            "risk_overrides": {"max_order_notional_usd": 3000},
        },
        {
            "id": "meanrev",
            "name": "zscore_revert",
            "enabled": True,
            "params": {"lookback": 20, "z_entry": 2.0},
            "universe": ["SPY", "QQQ"],
            "slots": [{"id": "am", "time": "10:15", "drift_max_minutes": 20}],
        },
    ],
    "risk": {
        "max_position_size_pct": 10,
        "max_order_notional_usd": 5000,
        "max_gross_exposure_usd": 25000,
        "daily_loss_limit_pct": 2,
        "max_trades_per_day": 6,
        "max_staleness_seconds": 60,
        "max_spread_pct": 1.0,
        "allowlist": ["AAPL", "MSFT", "SPY", "QQQ"],
        "enforce_pdt": True,
        "auto_flatten_on_kill": False,
        "conflict_policy": "net",
    },
    "execution": {"order_type": "market", "poll_timeout_seconds": 60, "rate_limit_per_min": 100},
    "backtest": {
        "start": "2022-01-01",
        "end": "2024-12-31",
        "data_vendor": "schwab",
        "fees_model": {"commission": 0, "regulatory_bps": 0.2},
        "slippage_model": {"type": "bps", "value": 2},
        "base_seed": 12345,
    },
    "alerting": {"channels": ["telegram", "email"], "heartbeat_minutes": 60},
    "observability": {
        "log_format": "json",
        "db_path": "/state/trader.sqlite",
        "data_cache": "/data/",
    },
}


def cfg(**overrides: Any) -> dict[str, Any]:
    """A deep copy of EXAMPLE with shallow top-level overrides applied."""
    d = copy.deepcopy(EXAMPLE)
    d.update(overrides)
    return d


def test_parses_example_config() -> None:
    c = AppConfig.model_validate(EXAMPLE)
    assert c.mode is Mode.PAPER
    assert len(c.strategies) == 2
    assert c.strategies[0].id == "momentum"
    assert c.risk.conflict_policy is ConflictPolicy.NET
    assert c.execution.order_type is OrderType.MARKET
    assert c.backtest is not None
    assert c.backtest.start.year == 2022


def test_config_is_frozen() -> None:
    c = AppConfig.model_validate(EXAMPLE)
    with pytest.raises(ValidationError):
        c.mode = Mode.LIVE  # type: ignore[misc]


def test_drift_ceiling() -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["strategies"][0]["slots"][0]["drift_max_minutes"] = 120
    with pytest.raises(ValidationError):
        AppConfig.model_validate(bad)


def test_unique_strategy_ids() -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["strategies"][1]["id"] = "momentum"  # duplicate
    with pytest.raises(ValidationError, match="unique"):
        AppConfig.model_validate(bad)


def test_risk_override_keys_subset() -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["strategies"][0]["risk_overrides"] = {"bogus_key": 1}
    with pytest.raises(ValidationError, match="risk_overrides"):
        AppConfig.model_validate(bad)


def test_risk_override_cannot_loosen_safety_floor() -> None:
    # A data-integrity / account-wide-only key is a valid RiskConfig field but is NOT
    # tunable per strategy -- a strategy must never weaken a safety floor.
    bad = copy.deepcopy(EXAMPLE)
    bad["strategies"][0]["risk_overrides"] = {"max_staleness_seconds": 999999}
    with pytest.raises(ValidationError, match="not tunable per strategy"):
        AppConfig.model_validate(bad)


def test_mode_and_conflict_enums_reject_invalid() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(cfg(mode="bogus"))
    bad = copy.deepcopy(EXAMPLE)
    bad["risk"]["conflict_policy"] = "bogus"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(bad)


def test_order_type_case_insensitive() -> None:
    for v in ("market", "MARKET", "Market"):
        c = AppConfig.model_validate(cfg(execution={"order_type": v}))
        assert c.execution.order_type is OrderType.MARKET
    c = AppConfig.model_validate(cfg(execution={"order_type": "limit"}))
    assert c.execution.order_type is OrderType.LIMIT


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        AppConfig.model_validate(cfg(schedule={"timezone": "Mars/Phobos"}))


def test_slot_time_format() -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["strategies"][0]["slots"][0]["time"] = "25:00"
    with pytest.raises(ValidationError, match="HH:MM"):
        AppConfig.model_validate(bad)


def test_extra_keys_forbidden() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(cfg(unexpected_top_level=True))


def test_daily_loss_limit_must_be_positive() -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["risk"]["daily_loss_limit_pct"] = 0
    with pytest.raises(ValidationError):
        AppConfig.model_validate(bad)


def test_rate_limit_ceiling() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(cfg(execution={"rate_limit_per_min": 200}))


def test_enabled_strategy_needs_universe_and_slots() -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["strategies"][0]["universe"] = []
    with pytest.raises(ValidationError, match="universe"):
        AppConfig.model_validate(bad)


def test_disabled_strategy_may_be_empty() -> None:
    d = copy.deepcopy(EXAMPLE)
    d["strategies"].append({"id": "off", "name": "threshold", "enabled": False})
    c = AppConfig.model_validate(d)
    assert c.strategies[-1].enabled is False


def test_at_least_one_strategy_required() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(cfg(strategies=[]))


def test_backtest_optional_and_date_order() -> None:
    no_bt = copy.deepcopy(EXAMPLE)
    no_bt.pop("backtest")
    assert AppConfig.model_validate(no_bt).backtest is None

    bad = copy.deepcopy(EXAMPLE)
    bad["backtest"]["end"] = "2021-01-01"  # before start
    with pytest.raises(ValidationError, match="end must be"):
        AppConfig.model_validate(bad)


def test_defaults_fill_missing_sections() -> None:
    minimal = {
        "strategies": [
            {
                "id": "m",
                "name": "threshold",
                "universe": ["AAPL"],
                "slots": [{"id": "am", "time": "09:45"}],
            }
        ]
    }
    c = AppConfig.model_validate(minimal)
    assert c.mode is Mode.PAPER
    assert c.risk.enforce_pdt is True
    assert c.execution.rate_limit_per_min == 100
    assert c.schedule.timezone == "America/New_York"
