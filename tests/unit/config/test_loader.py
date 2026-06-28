"""Tests for the layered config loader: precedence (defaults < file < env < CLI),
deep-merge + env-parsing units, env string coercion, and per-leaf provenance."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from trader.config import DEFAULT_CONFIG_PATH, load_config, resolved_sources
from trader.config.loader import deep_merge, env_to_nested
from trader.core.enums import Mode, OrderType

MINIMAL_YAML = """
strategies:
  - id: m
    name: threshold
    universe: [AAPL]
    slots:
      - { id: am, time: "09:45" }
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# --- deep_merge / env_to_nested units --------------------------------------- #


def test_deep_merge_merges_nested_and_replaces_lists() -> None:
    base = {"risk": {"a": 1, "b": 2}, "universe": ["X"]}
    over = {"risk": {"b": 3, "c": 4}, "universe": ["Y", "Z"]}
    out = deep_merge(base, over)
    assert out == {"risk": {"a": 1, "b": 3, "c": 4}, "universe": ["Y", "Z"]}


def test_env_to_nested_builds_lowercased_tree() -> None:
    env = {"TRADER__RISK__MAX_TRADES_PER_DAY": "8", "OTHER": "ignored", "TRADER__MODE": "live"}
    assert env_to_nested(env) == {"risk": {"max_trades_per_day": "8"}, "mode": "live"}


def test_env_to_nested_scalar_conflict_raises() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        env_to_nested({"TRADER__RISK": "x", "TRADER__RISK__MAX_TRADES_PER_DAY": "8"})


# --- load_config precedence ------------------------------------------------- #


def test_loads_default_yaml() -> None:
    c = load_config(DEFAULT_CONFIG_PATH, environ={})
    assert c.mode is Mode.PAPER
    assert len(c.strategies) >= 1


def test_env_overrides_file() -> None:
    c = load_config(
        DEFAULT_CONFIG_PATH,
        environ={"TRADER__RISK__MAX_TRADES_PER_DAY": "8"},
    )
    assert c.risk.max_trades_per_day == 8  # file said 6


def test_cli_overrides_env() -> None:
    c = load_config(
        DEFAULT_CONFIG_PATH,
        environ={"TRADER__RISK__MAX_TRADES_PER_DAY": "8"},
        cli_overrides={"risk": {"max_trades_per_day": 9}},
    )
    assert c.risk.max_trades_per_day == 9


def test_defaults_when_absent(tmp_path: Path) -> None:
    c = load_config(_write(tmp_path, MINIMAL_YAML), environ={})
    # nothing in the file for these → model defaults
    assert c.mode is Mode.PAPER
    assert c.schedule.timezone == "America/New_York"
    assert c.risk.enforce_pdt is True
    assert c.execution.rate_limit_per_min == 100


def test_env_string_coercion(tmp_path: Path) -> None:
    c = load_config(
        _write(tmp_path, MINIMAL_YAML),
        environ={
            "TRADER__SCHEDULE__CATCH_UP": "true",
            "TRADER__EXECUTION__POLL_TIMEOUT_SECONDS": "30",
        },
    )
    assert c.schedule.catch_up is True
    assert c.execution.poll_timeout_seconds == 30


def test_invalid_merged_config_raises() -> None:
    with pytest.raises(ValidationError):
        load_config(
            DEFAULT_CONFIG_PATH,
            environ={},
            cli_overrides={"execution": {"rate_limit_per_min": 999}},
        )


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="top-level mapping"):
        load_config(p, environ={})


def test_no_file_uses_env_and_cli_only() -> None:
    # No file: model defaults + env + cli. Provide strategies via cli to satisfy min_length.
    c = load_config(
        None,
        environ={},
        cli_overrides={
            "strategies": [
                {
                    "id": "m",
                    "name": "threshold",
                    "universe": ["AAPL"],
                    "slots": [{"id": "am", "time": "09:45"}],
                }
            ]
        },
    )
    assert c.strategies[0].id == "m"


# --- provenance ------------------------------------------------------------- #


def test_resolved_sources_reports_winning_layer() -> None:
    src = resolved_sources(
        DEFAULT_CONFIG_PATH,
        environ={"TRADER__RISK__MAX_TRADES_PER_DAY": "8"},
        cli_overrides={"execution": {"order_type": "limit"}},
    )
    assert src["mode"] == "file"  # set in default.yaml
    assert src["risk.max_trades_per_day"] == "env"  # env overrode file
    assert src["execution.order_type"] == "cli"  # cli overrode all


def test_order_type_override_via_cli_is_coerced() -> None:
    c = load_config(
        DEFAULT_CONFIG_PATH, environ={}, cli_overrides={"execution": {"order_type": "limit"}}
    )
    assert c.execution.order_type is OrderType.LIMIT
