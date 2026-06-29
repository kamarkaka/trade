"""Static exposure assertions on deploy/docker-compose.yml (M7.11 / §16.6): only the proxy
publishes a host port (:443); the web is internal-only; the trader is private; the internal
network is truly internal. A CI gate so a future edit can't accidentally publish the web/DB."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_only_proxy_publishes_ports(compose: dict[str, Any]) -> None:
    services = compose["services"]
    publishing = {name: svc.get("ports") for name, svc in services.items() if svc.get("ports")}
    assert set(publishing) == {"proxy"}, f"unexpected published services: {publishing}"


def test_proxy_publishes_only_443(compose: dict[str, Any]) -> None:
    assert compose["services"]["proxy"]["ports"] == ["443:443"]


def test_web_is_internal_only(compose: dict[str, Any]) -> None:
    web = compose["services"]["web"]
    assert "ports" not in web, "web must NOT publish a host port"
    assert [str(p) for p in web["expose"]] == ["8000"]  # internal expose only


def test_trader_not_published(compose: dict[str, Any]) -> None:
    assert "ports" not in compose["services"]["trader"]


def test_internal_network_is_internal(compose: dict[str, Any]) -> None:
    assert compose["networks"]["internal"]["internal"] is True


def test_network_membership(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert set(services["trader"]["networks"]) == {"internal"}  # private, no edge
    assert set(services["web"]["networks"]) == {"internal", "edge"}
    assert set(services["proxy"]["networks"]) == {"edge"}  # never on internal


def test_web_shares_state_readonly_mount(compose: dict[str, Any]) -> None:
    # web mounts the shared state volume (its handle is mode=ro at the connection level, M7.1)
    # and the config read-only.
    mounts = compose["services"]["web"]["volumes"]
    assert any(m.startswith("trader_state:/state") for m in mounts)
    assert any(":/config/" in m and m.endswith(":ro") for m in mounts)


def test_web_secrets_via_env_file_not_inline(compose: dict[str, Any]) -> None:
    web = compose["services"]["web"]
    assert "./secrets/.env" in web["env_file"]
    env = web.get("environment", {})
    keys = env.keys() if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
    for key in keys:
        assert not any(tok in key.upper() for tok in ("PASSWORD", "SECRET", "TOKEN")), key


def test_proxy_mounts_caddyfile_readonly(compose: dict[str, Any]) -> None:
    mounts = compose["services"]["proxy"]["volumes"]
    assert any(m.endswith("/etc/caddy/Caddyfile:ro") for m in mounts)
