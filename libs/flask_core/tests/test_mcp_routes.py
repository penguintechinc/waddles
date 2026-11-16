"""
MCP transport tests (Streamable HTTP, JSON response mode)
=============================================================

Exercises `mcp_routes.mcp_bp` end-to-end via `app.test_client()` --
real JWTs (`auth.create_jwt_token`), a real in-memory pydal `tenants`
table (mirrors `test_tenancy.py`'s fixture), and the singleton
Feature/App registries (cleared and reseeded per test, same pattern
`test_feature_registry.py`'s `test_entitled_features_defaults_to_singleton_registry_pool`
uses) -- because this is the transport that actually calls
`list_tools_for_tenant`/`authorize_and_resolve_tool_call` with no
registry override, the same way hub-api will.

Fail-first proof: `test_tools_call_denied_for_wrong_tenant` (unknown/
inactive tenant claim) was verified to catch a regression by temporarily
making `_resolve_identity` skip the `resolve_tenant_context` call and
trust the tenant claim as-is -- the test went red (200 instead of 403),
then the skip was reverted.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

import pytest
from pydal import DAL, Field
from quart import Quart

from flask_core.app_registry import get_registry as get_app_registry
from flask_core.auth import create_jwt_token
from flask_core.feature_contract import FeatureContract
from flask_core.feature_registry import get_registry as get_feature_registry
from flask_core.mcp_routes import mcp_bp

SECRET = "test-secret-key-not-for-production-use-only"

FREE_CONTRACT = FeatureContract(
    id="bot.shoutout",
    version=1,
    module="bot",
    requires_scopes=frozenset({"bot.command:write"}),
    min_tier="free",
    flag="waddles.bot.shoutout",
)
ENTERPRISE_CONTRACT = FeatureContract(
    id="bot.enterprise_thing",
    version=1,
    module="bot",
    requires_scopes=frozenset({"bot.command:admin"}),
    min_tier="enterprise",
    flag="waddles.bot.enterprise_thing",
)


@pytest.fixture
def db() -> Any:
    dal = DAL("sqlite:memory")
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("is_active", "boolean", default=True),
    )
    yield dal
    dal.close()


@pytest.fixture
def tenants(db: Any) -> dict[str, int]:
    free_id = db.tenants.insert(slug="free-co", is_active=True)
    ent_id = db.tenants.insert(slug="ent-co", is_active=True)
    db.tenants.insert(slug="disabled-co", is_active=False)
    db.commit()
    return {"free-co": free_id, "ent-co": ent_id}


@pytest.fixture
def app(db: Any) -> Quart:
    quart_app = Quart(__name__)
    quart_app.register_blueprint(mcp_bp)
    quart_app.config["dal"] = db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def seeded_registries(tenants: dict[str, int]) -> AsyncIterator[None]:
    """Clear + reseed the singleton Feature/App registries -- this transport
    calls `list_tools_for_tenant`/`authorize_and_resolve_tool_call` with no
    registry override, same as hub-api will, so tests must exercise the
    actual singleton path, not an isolated instance."""
    feature_registry = get_feature_registry()
    app_registry = get_app_registry()
    feature_registry.clear()
    app_registry.clear()
    feature_registry.register(FREE_CONTRACT)
    feature_registry.register(ENTERPRISE_CONTRACT)
    app_registry.load(
        {
            "app_id": "waddles.bot.shoutout.default",
            "name": "Shoutout (default)",
            "version": "1.0.0",
            "feature": "waddles.bot.shoutout",
            "module": "bot",
            "provider": "builtin",
            "is_default": True,
        }
    )
    app_registry.load(
        {
            "app_id": "waddles.bot.enterprise_thing.default",
            "name": "Enterprise Thing (default)",
            "version": "1.0.0",
            "feature": "waddles.bot.enterprise_thing",
            "module": "bot",
            "provider": "builtin",
            "is_default": True,
        }
    )
    yield
    feature_registry.clear()
    app_registry.clear()


def token_for(tenant: str, roles: list[str]) -> str:
    return create_jwt_token(
        user_id="u1",
        username="alice",
        email="alice@example.com",
        roles=roles,
        tenant=tenant,
    )


def auth_headers(tenant: str, roles: list[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(tenant, roles)}"}


class _FakeFeatureEnabled:
    """Stands in for the full two-gate flag+license evaluation -- resolves per fixed set."""

    def __init__(self, enabled_flags: set[str]) -> None:
        self.enabled_flags = enabled_flags

    async def __call__(
        self,
        flag_key: str,
        *,
        tenant: str,
        community: Optional[int] = None,
        default: bool = False,
    ) -> bool:
        return flag_key in self.enabled_flags


@pytest.fixture(autouse=True)
def secret_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mcp_routes._resolve_identity` reads SECRET_KEY from the environment
    (same as `tenancy.tenant_middleware`) -- must match what signs `SECRET` tokens."""
    monkeypatch.setenv("SECRET_KEY", SECRET)


@pytest.fixture(autouse=True)
def patch_feature_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both `authorize_and_resolve_tool_call` and `entitled_features` locally
    re-import `flask_core.feature_flags.feature_enabled` on every call when no
    `check` is injected (the routes module never injects one), so patching the
    module attribute here reaches both -- no live PostHog/license connection."""
    fake = _FakeFeatureEnabled(
        enabled_flags={"waddles.bot.shoutout", "waddles.bot.enterprise_thing"}
    )
    monkeypatch.setattr("flask_core.feature_flags.feature_enabled", fake)


class TestAuthentication:
    async def test_missing_bearer_token_rejected(self, client: Any) -> None:
        response = await client.post(
            "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401
        body = await response.get_json()
        assert body["error"]["message"] == "Authentication required"

    async def test_tools_call_denied_for_wrong_tenant(self, client: Any) -> None:
        """Fail-first proof performed against this test -- see module docstring.
        A token whose tenant claim resolves to no active tenant row must never
        reach tools/call -- this is the 'tenant mismatch' rejection path."""
        headers = auth_headers("no-such-tenant", roles=["global:admin"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bot.shoutout@1", "arguments": {}},
            },
        )
        assert response.status_code == 403

    async def test_inactive_tenant_rejected(self, client: Any) -> None:
        headers = auth_headers("disabled-co", roles=["global:admin"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 403


class TestInitialize:
    async def test_initialize_returns_protocol_version(self, client: Any) -> None:
        headers = auth_headers("free-co", roles=["community:viewer"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["result"]["protocolVersion"]
        assert body["result"]["capabilities"]["tools"] is not None

    async def test_notification_acknowledged_without_body(self, client: Any) -> None:
        headers = auth_headers("free-co", roles=["community:viewer"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert response.status_code == 202

    async def test_unknown_method_returns_method_not_found(self, client: Any) -> None:
        headers = auth_headers("free-co", roles=["community:viewer"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "not/a/method"},
        )
        assert response.status_code == 404
        body = await response.get_json()
        assert body["error"]["code"] == -32601


class TestToolsListPerTenant:
    async def test_free_tenant_sees_only_free_tool(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Override the autouse both-flags-on fake -- this tenant's license tier
        # entitles only the free flag (production tier gating lives in
        # entitlement.py; this fake simulates its net two-gate verdict).
        monkeypatch.setattr(
            "flask_core.feature_flags.feature_enabled",
            _FakeFeatureEnabled(enabled_flags={"waddles.bot.shoutout"}),
        )
        headers = auth_headers("free-co", roles=["community:viewer"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        body = await response.get_json()
        names = {t["name"] for t in body["result"]["tools"]}
        assert names == {"bot.shoutout@1"}
        # Load-bearing: the Enterprise tool must not leak to a Free tenant's listing.
        assert "bot.enterprise_thing@1" not in names

    async def test_enterprise_tenant_sees_both_tools(self, client: Any) -> None:
        # This tenant's license tier entitles both flags -- simulated directly via
        # the fake (see patch_feature_enabled); a per-tenant fake would be a more
        # faithful two-gate simulation, but flag resolution here is tenant-blind by
        # design (production tenant-vs-tier gating lives in entitlement.py, not here).
        headers = auth_headers("ent-co", roles=["community:viewer"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        body = await response.get_json()
        names = {t["name"] for t in body["result"]["tools"]}
        assert names == {"bot.shoutout@1", "bot.enterprise_thing@1"}


class TestToolsCallGate:
    async def test_denied_when_scope_missing(self, client: Any) -> None:
        headers = auth_headers(
            "free-co", roles=["community:viewer"]
        )  # no bot.command:write
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bot.shoutout@1", "arguments": {}},
            },
        )
        assert response.status_code == 200  # JSON-RPC error body, not an HTTP failure
        body = await response.get_json()
        assert body["error"]["code"] == -32002
        assert body["error"]["data"]["reason"] == "scope_denied"

    async def test_denied_when_feature_disabled(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "flask_core.feature_flags.feature_enabled",
            _FakeFeatureEnabled(enabled_flags=set()),
        )
        headers = auth_headers("free-co", roles=["global:admin"])  # scope is fine
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bot.shoutout@1", "arguments": {}},
            },
        )
        body = await response.get_json()
        assert body["error"]["code"] == -32003
        assert body["error"]["data"]["reason"] == "feature_disabled"

    async def test_denied_for_unknown_tool(self, client: Any) -> None:
        headers = auth_headers("free-co", roles=["global:admin"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bot.does-not-exist@1", "arguments": {}},
            },
        )
        body = await response.get_json()
        assert body["error"]["code"] == -32001
        assert body["error"]["data"]["reason"] == "unknown_tool"

    async def test_enterprise_tool_call_denied_when_flag_off_for_tenant(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A direct tools/call for a tool this tenant's flag doesn't entitle is
        blocked below the surface -- not merely hidden from tools/list (design
        doc: gates only on the transport is the same breach REST would be)."""
        monkeypatch.setattr(
            "flask_core.feature_flags.feature_enabled",
            _FakeFeatureEnabled(
                enabled_flags={"waddles.bot.shoutout"}
            ),  # enterprise flag off
        )
        headers = auth_headers(
            "free-co", roles=["global:admin"]
        )  # scope is not the blocker here
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bot.enterprise_thing@1", "arguments": {}},
            },
        )
        body = await response.get_json()
        assert body["error"]["code"] == -32003
        assert body["error"]["data"]["reason"] == "feature_disabled"

    async def test_success_dispatches_to_resolved_app(self, client: Any) -> None:
        headers = auth_headers("free-co", roles=["global:admin"])
        response = await client.post(
            "/mcp/v1",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "bot.shoutout@1",
                    "arguments": {"arguments": {"message": "hi"}},
                },
            },
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["result"]["isError"] is False
        text = body["result"]["content"][0]["text"]
        assert "bot.shoutout@1" in text
        assert "waddles.bot.shoutout.default" in text
