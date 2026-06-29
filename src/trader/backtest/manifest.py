"""Reproducibility manifest for a backtest run (design §9.5).

Captures everything needed to exactly re-derive a result: a portable ``config_hash``
(sha256 over the canonical sorted-key JSON of the resolved AppConfig), the cache
``data_hashes`` (content-addressed), the ``seed``, the git commit, and python/lib
versions. Written alongside each run's report (M2.10). The hash canonicalization is
defined so it is stable across machines and Python minor versions.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

from trader.config import AppConfig

# Libraries whose versions materially affect numeric results.
_TRACKED_LIBS = ("pydantic", "pandas", "pyarrow", "numpy")

# Deployment/notification subtrees that don't affect backtest results — excluded so
# the config_hash is portable across environments (local vs CI golden run, M2.10).
_HASH_EXCLUDE = {"observability": True, "alerting": True}

# Project root (…/src/trader/backtest/manifest.py -> repo root), so the recorded
# git commit is the project's, not whatever repo the process CWD happens to be in.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def config_hash(config: AppConfig) -> str:
    """SHA-256 of the canonicalized (sorted-key, compact JSON) resolved config.

    Result-affecting fields only (observability/alerting excluded). Note the hash is
    sensitive to a field's numeric *formatting* in the source (e.g. ``5000`` vs
    ``5000.0``); a fixed config fixture (the golden run) keeps it stable.
    """
    canonical = json.dumps(
        config.model_dump(mode="json", exclude=_HASH_EXCLUDE),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _lib_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _TRACKED_LIBS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "unknown"
    return versions


def build_manifest(config: AppConfig, data_hashes: dict[str, str], seed: int) -> dict[str, Any]:
    """Assemble the reproducibility manifest for a run."""
    return {
        "config_hash": config_hash(config),
        "data_hashes": dict(sorted(data_hashes.items())),
        "seed": seed,
        "git_commit": _git_commit(),
        "python_version": platform.python_version(),
        "lib_versions": _lib_versions(),
    }


def write_manifest(manifest: dict[str, Any], path: str | Path) -> None:
    """Write the manifest as pretty, stable JSON."""
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
