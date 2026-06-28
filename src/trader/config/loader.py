"""Layered configuration loader (design §11).

Precedence, lowest to highest: **model defaults < YAML file < environment < CLI**.
Layers are deep-merged at the leaf level (nested dicts merge; lists and scalars
replace), then validated into a single immutable :class:`AppConfig` that drives
both live and backtest.

Environment overrides use a nested convention: ``TRADER__SECTION__KEY=value``
(e.g. ``TRADER__RISK__MAX_TRADES_PER_DAY=8``). Values arrive as strings and are
coerced by pydantic during validation.

Secrets are never read here — config holds only *references* to where credentials
live (design §13); the secrets component (M1) resolves them separately.

A small manual loader is used rather than ``pydantic-settings`` so the deep-merge
semantics, precedence, and per-leaf provenance (:func:`resolved_sources`) are
explicit and easy to test.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig

# The shipped, runnable example config (repo root); the CLI defaults to this.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "default.yaml"

_ENV_PREFIX = "TRADER"
_ENV_DELIM = "__"


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` deep-merged with ``override`` (override wins).

    Nested dicts merge recursively; lists and scalars replace wholesale.
    """
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _load_yaml(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"config file {path} must contain a top-level mapping, got {type(data).__name__}"
        )
    return data


def env_to_nested(
    environ: Mapping[str, str],
    *,
    prefix: str = _ENV_PREFIX,
    delimiter: str = _ENV_DELIM,
) -> dict[str, Any]:
    """Turn ``PREFIX__A__B=value`` environment variables into ``{a: {b: value}}``.

    Path segments are lower-cased to match config field names. Values stay strings
    (pydantic coerces them at validation time).
    """
    full_prefix = f"{prefix}{delimiter}"
    result: dict[str, Any] = {}
    for raw_key, raw_value in environ.items():
        if not raw_key.startswith(full_prefix):
            continue
        parts = [p.lower() for p in raw_key[len(full_prefix) :].split(delimiter)]
        if not parts or any(p == "" for p in parts):
            continue
        node = result
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = {}
                node[part] = child
            elif not isinstance(child, dict):
                raise ValueError(f"env var {raw_key} conflicts with a scalar at {part!r}")
            node = child
        node[parts[-1]] = raw_value
    return result


def _assembled_layers(
    path: str | Path | None,
    cli_overrides: Mapping[str, Any] | None,
    environ: Mapping[str, str] | None,
    prefix: str,
    delimiter: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Return (layer_name, dict) pairs in ascending precedence order."""
    layers: list[tuple[str, dict[str, Any]]] = []
    if path is not None:
        layers.append(("file", _load_yaml(path)))
    env = environ if environ is not None else os.environ
    layers.append(("env", env_to_nested(env, prefix=prefix, delimiter=delimiter)))
    if cli_overrides:
        layers.append(("cli", dict(cli_overrides)))
    return layers


def load_config(
    path: str | Path | None = None,
    *,
    cli_overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    env_prefix: str = _ENV_PREFIX,
    env_nested_delimiter: str = _ENV_DELIM,
) -> AppConfig:
    """Load, deep-merge, and validate config into an :class:`AppConfig`.

    Precedence: model defaults < ``path`` YAML < environment < ``cli_overrides``.
    """
    layers = _assembled_layers(path, cli_overrides, environ, env_prefix, env_nested_delimiter)
    merged: dict[str, Any] = {}
    for _name, layer in layers:
        merged = deep_merge(merged, layer)
    return AppConfig.model_validate(merged)


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts to dotted-path leaves (lists/scalars are leaves)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, dotted))
        else:
            out[dotted] = value
    return out


def resolved_sources(
    path: str | Path | None = None,
    *,
    cli_overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    env_prefix: str = _ENV_PREFIX,
    env_nested_delimiter: str = _ENV_DELIM,
) -> dict[str, str]:
    """Map each explicitly-set leaf path to the layer that set it (debugging).

    Leaves present in none of the explicit layers come from model defaults and are
    omitted here.
    """
    layers = _assembled_layers(path, cli_overrides, environ, env_prefix, env_nested_delimiter)
    sources: dict[str, str] = {}
    for name, layer in layers:  # ascending precedence → later wins
        for leaf in _flatten(layer):
            sources[leaf] = name
    return sources


__all__ = ["DEFAULT_CONFIG_PATH", "deep_merge", "env_to_nested", "load_config", "resolved_sources"]
