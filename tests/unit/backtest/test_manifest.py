"""Tests for the run manifest (canonical config hash, fields) and seeded RNG (M2.9)."""

from pathlib import Path

import numpy as np
import yaml

from trader.backtest.manifest import build_manifest, config_hash, write_manifest
from trader.backtest.rng import make_rng
from trader.config import DEFAULT_CONFIG_PATH, load_config


def _config():
    return load_config(DEFAULT_CONFIG_PATH)


def test_config_hash_canonical(tmp_path: Path) -> None:
    # the same config serialized with different YAML key orders hashes identically
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(yaml.safe_dump(raw, sort_keys=True), encoding="utf-8")
    b.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert config_hash(load_config(a)) == config_hash(load_config(b))


def test_config_hash_changes_with_content(tmp_path: Path) -> None:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    base = config_hash(load_config(DEFAULT_CONFIG_PATH))
    raw["mode"] = "backtest" if raw.get("mode") != "backtest" else "paper"
    changed = tmp_path / "c.yaml"
    changed.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert config_hash(load_config(changed)) != base


def test_manifest_has_all_fields() -> None:
    m = build_manifest(_config(), {"AAPL": "deadbeef"}, seed=42)
    assert set(m) >= {
        "config_hash",
        "data_hashes",
        "seed",
        "git_commit",
        "python_version",
        "lib_versions",
    }
    assert m["seed"] == 42
    assert m["data_hashes"] == {"AAPL": "deadbeef"}
    assert m["lib_versions"]["numpy"] != "unknown"  # numpy is an installed dependency


def test_write_manifest_roundtrips(tmp_path: Path) -> None:
    import json

    m = build_manifest(_config(), {"AAPL": "deadbeef"}, seed=7)
    out = tmp_path / "manifest.json"
    write_manifest(m, out)
    assert json.loads(out.read_text()) == m


def test_rng_is_seeded_not_global() -> None:
    # reproducible from the seed, and independent of the global numpy RNG
    assert make_rng(42).random() == make_rng(42).random()  # same seed -> same draw
    assert make_rng(42).random() != make_rng(43).random()  # different seed -> different
    expected = make_rng(42).random()
    np.random.seed(999)  # perturb the global RNG
    _ = np.random.random()
    assert make_rng(42).random() == expected  # unaffected by the global RNG


def test_manifest_is_deterministic_across_builds() -> None:
    cfg = _config()
    a = build_manifest(cfg, {"AAPL": "h"}, seed=1)
    b = build_manifest(cfg, {"AAPL": "h"}, seed=1)
    assert a == b  # same inputs -> identical manifest (incl. git/lib/python)
