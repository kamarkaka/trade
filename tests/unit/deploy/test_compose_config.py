"""Static assertions on deploy/docker-compose.yml: durable named volumes, restart policy,
and that secrets come from env_file (never inlined) (M4.9)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"
_SECRET_TOKENS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def service(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]["trader"]


def test_named_volumes_present(compose: dict[str, Any], service: dict[str, Any]) -> None:
    # Declared as named volumes (so they persist) AND mounted at /state and /data.
    assert {"trader_state", "trader_data"} <= set(compose["volumes"])
    mounts = service["volumes"]
    assert any(m.startswith("trader_state:/state") for m in mounts)
    assert any(m.startswith("trader_data:/data") for m in mounts)


def test_config_mounted_read_only(service: dict[str, Any]) -> None:
    config_mounts = [m for m in service["volumes"] if ":/config/" in m]
    assert config_mounts and all(m.endswith(":ro") for m in config_mounts)


def test_restart_policy_unless_stopped(service: dict[str, Any]) -> None:
    assert service["restart"] == "unless-stopped"


def test_env_file_referenced_not_inline_secrets(service: dict[str, Any]) -> None:
    assert "./secrets/.env" in service["env_file"]
    # The inline `environment` must carry NO credentials — those come from the env_file.
    env = service.get("environment", {})
    keys = env.keys() if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
    for key in keys:
        assert not any(tok in key.upper() for tok in _SECRET_TOKENS), f"secret inlined: {key}"


def test_healthcheck_uses_status_healthcheck(service: dict[str, Any]) -> None:
    test = service["healthcheck"]["test"]
    assert "--healthcheck" in test and "status" in test


def test_logging_rotation_configured(service: dict[str, Any]) -> None:
    opts = service["logging"]["options"]
    assert opts["max-size"] and int(opts["max-file"]) >= 1


def test_resource_limits_enforced_under_compose_up(service: dict[str, Any]) -> None:
    # Top-level mem_limit/cpus are what `docker compose up` actually enforces (the
    # deploy.resources block is swarm-only and a no-op under `up`).
    assert service["mem_limit"] and service["cpus"]


def test_mounted_config_writes_land_on_named_volumes() -> None:
    # The durability invariant: the MOUNTED config must point db_path under /state and
    # data_cache under /data, else the named volumes are mounted but unused (state lost on
    # recreate). The compose binds config/default.yaml -> /config/trader.yaml.
    cfg = yaml.safe_load(
        (COMPOSE_PATH.parents[1] / "config" / "default.yaml").read_text(encoding="utf-8")
    )
    obs = cfg["observability"]
    assert obs["db_path"].startswith("/state"), obs["db_path"]
    assert obs["data_cache"].startswith("/data"), obs["data_cache"]
