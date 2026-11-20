"""Regression: the remaining alpha 402s (#1466 sweep): standard body, bypass-aware, baselines.

* Every community-feature gate answers 402 with `error.code == FEATURE_NOT_ENABLED` (hub-webui keys
  its "Upgrade to Premium" notice on it) -- chat, interaction channels, inventory and raffles used
  to answer a bare `ERROR_402` / code-less body.
* Alpha's `*.penguintech.cloud[:port]` is a license-bypass domain: the TIER gate passes there, the
  FLAG gate never does -- so free-tier flags need explicit `FLAG_*` baselines in
  `values-dal2alpha.yaml`, and licensed flags must NOT be there (env can never stand in for them).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from flask_core import entitlement
from flask_core.entitlement import EntitlementClient, env_flag_var_name
from flask_core.tier_catalog import FEATURE_MIN_TIERS
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_chat import chat_bp
from blueprints.v1.community_interaction import interaction_admin_bp
from blueprints.v1.community_inventory import inventory_bp
from blueprints.v1.community_raffle import raffle_bp
from tests.community_ids import cpath, install_community_ids

ALPHA_HOST = "waddles-alpha.penguintech.cloud:8443"
CUSTOMER_HOST = "customer.example.com"

VALUES = Path(__file__).resolve().parents[2] / "k8s/helm/waddlebot/values-dal2alpha.yaml"

#: (blueprint, flag, path, scope) -- the endpoints whose 402 body/bypass this PR pins.
ENDPOINTS = [
    (
        chat_bp,
        "waddles.community.chat",
        "/api/v1/community/{community_id}/chat/history",
        "community.chat:read",
    ),
    (
        interaction_admin_bp,
        "waddles.community.interactions",
        "/api/v1/admin/{community_id}/interaction/channels",
        "users:admin",
    ),
    (
        inventory_bp,
        "waddles.community.inventory",
        "/api/v1/admin/{community_id}/inventory/items",
        "community.inventory:read",
    ),
    (
        raffle_bp,
        "waddles.community.raffles",
        "/api/v1/admin/{community_id}/raffle-customization",
        "community.raffle:read",
    ),
]

#: Every flag the swept endpoints gate on; licensed ones are identified by the tier catalog.
SWEPT_FLAGS = [
    "waddles.streaming.overlays",
    "waddles.streaming.overlay-builder",
    "waddles.streaming.rtc",
    "waddles.event.calendar",
    "waddles.community.chat",
    "waddles.community.interactions",
    "waddles.community.loyalty",
    "waddles.community.inventory",
    "waddles.community.raffles",
    "waddles.analytics.community_health",
]


class _Flag:
    """PostHog stand-in answering one fixed value."""

    def __init__(self, value: bool | None) -> None:
        self.value = value

    def is_enabled(self, *_a: Any, **_k: Any) -> bool | None:
        return self.value


class _License:
    """Tenant license-tier stand-in (a Free tenant)."""

    def resolve_tier(self) -> str:
        return "free"


@pytest.fixture
def entitlement_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a real `EntitlementClient` (tenant Free) as the process-wide client."""

    def _install(flag: bool | None = True) -> EntitlementClient:
        client = EntitlementClient(flag_gate=_Flag(flag), license_gate=_License())
        monkeypatch.setattr(entitlement, "_default_client", client)
        return client

    return _install


@pytest.fixture(autouse=True)
def _inventory_authz_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise inventory's per-community authz: this file pins the TIER/FLAG gate only.

    The authz gate (member/admin, tenant_admins + role tables the minimal `community_db` here does
    not build) is covered end-to-end in `test_community_inventory.py`.
    """
    import blueprints.v1.community_inventory as inventory_module

    monkeypatch.setattr(inventory_module, "require_inventory_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(inventory_module, "require_inventory_member", lambda *_a, **_k: None)


def _app(bp: Any, dal: Any) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    install_community_ids(app, dal)
    app.register_blueprint(bp)
    app.config["dal"] = dal
    return app


@pytest.mark.parametrize(("bp", "flag", "path", "scope"), ENDPOINTS, ids=[e[1] for e in ENDPOINTS])
async def test_bypass_domain_passes_and_free_tenant_elsewhere_gets_standard_402(
    community_db: Any,
    auth_headers: Any,
    entitlement_client: Any,
    bp: Any,
    flag: str,
    path: str,
    scope: str,
) -> None:
    """Alpha host (with :port) passes the tier gate; a Free tenant on a customer host is refused."""
    dal, community_id = community_db
    client = _app(bp, dal).test_client()
    url = cpath(dal, path, community_id)
    licensed = flag in FEATURE_MIN_TIERS

    def hdrs(host: str) -> dict[str, str]:
        return {**auth_headers(scope=scope, user_id="1"), "Host": host}

    entitlement_client(True)

    alpha = await client.get(url, headers=hdrs(ALPHA_HOST))
    assert alpha.status_code == 200

    other = await client.get(url, headers=hdrs(CUSTOMER_HOST))
    if licensed:
        assert other.status_code == 402
        body = await other.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "FEATURE_NOT_ENABLED"
        assert body["error"]["message"]
    else:
        assert other.status_code == 200  # free feature, flag ON: tier never gates it

    entitlement_client(False)  # the flag gate is never skipped, even on a bypass domain
    off = await client.get(url, headers=hdrs(ALPHA_HOST))
    assert off.status_code == 402
    assert (await off.get_json())["error"]["code"] == "FEATURE_NOT_ENABLED"


def test_alpha_flag_baselines_cover_free_flags_and_never_licensed_ones() -> None:
    """Free swept flags are env-baselined on alpha; catalog-licensed flags must not be."""
    doc = yaml.safe_load(VALUES.read_text())
    env = _find_extra_env(doc)
    assert env, "no FLAG_* extraEnv found in values-dal2alpha.yaml"
    for flag in SWEPT_FLAGS:
        name = env_flag_var_name(flag)
        if flag in FEATURE_MIN_TIERS:
            assert name not in env, f"{flag} is licensed: env can never grant it"
        else:
            assert str(env.get(name)).lower() == "true", f"{flag} missing {name} baseline"


def _find_extra_env(node: Any) -> dict[str, Any]:
    """Locate the hub-api `extraEnv` map that carries the FLAG_* baselines."""
    if isinstance(node, dict):
        extra = node.get("extraEnv")
        if isinstance(extra, dict) and any(str(k).startswith("FLAG_") for k in extra):
            return extra
        for value in node.values():
            found = _find_extra_env(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_extra_env(value)
            if found:
                return found
    return {}
