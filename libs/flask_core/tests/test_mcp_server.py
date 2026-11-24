"""
MCP surface (core) tests
===========================

Covers :mod:`flask_core.mcp_server`: tool derivation from Feature
contracts, per-tenant ``tools/list`` filtering, and the below-the-surface
``tools/call`` gate chain (scope -> feature/licensing -> App resolution).
Transport-level (JSON-RPC over Quart) tests live in
``test_mcp_routes.py``.

Fail-first proof (design doc "One authorization path, or MCP becomes the
bypass" / task spec "fail-first proven"):

- ``test_authorize_denies_when_scope_missing`` was verified to catch a
  regression by temporarily short-circuiting ``has_required_scopes`` in
  ``authorize_and_resolve_tool_call`` to always return ``True`` -- the
  test went red (``McpAuthorizationError`` never raised), then the
  short-circuit was reverted.
- ``test_list_tools_for_tenant_excludes_disabled_feature`` (the Free-vs-
  Enterprise leak test) was verified to catch a regression by temporarily
  making ``list_tools_for_tenant`` return every contract in the pool
  unconditionally (bypassing ``entitled_features``) -- the test went red
  (the enterprise-only tool appeared for the free tenant), then reverted.

Both fail-first runs are reproducible via the sed toggle in the PR
description; not left in the tree as skipped/xfail tests.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from flask_core.app_binding import AppInstallation
from flask_core.app_registry import AppRegistry
from flask_core.feature_contract import FeatureContract
from flask_core.feature_registry import FeatureRegistry
from flask_core.mcp_server import (
    REASON_FEATURE_DISABLED,
    REASON_NO_APP_BOUND,
    REASON_SCOPE_DENIED,
    REASON_UNKNOWN_TOOL,
    McpAuthorizationError,
    effective_scopes,
    has_required_scopes,
    list_tools_for_tenant,
    mcp_tool_from_contract,
    tool_name_for_contract,
)


def make_contract(
    id: str,
    module: str = "bot",
    min_tier: str = "free",
    requires_scopes: frozenset[str] = frozenset({"bot.command:write"}),
    version: int = 1,
) -> FeatureContract:
    return FeatureContract(
        id=id,
        version=version,
        module=module,
        requires_scopes=requires_scopes,
        min_tier=min_tier,
        flag=f"waddles.{id}",
    )


class FakeCheck:
    """Resolves each (flag, tenant, community) against a fixed enabled-flags set."""

    def __init__(self, enabled_flags: set[str]) -> None:
        self.enabled_flags = enabled_flags
        self.calls: List[tuple[str, str, Optional[int]]] = []

    async def __call__(
        self,
        flag_key: str,
        *,
        tenant: str,
        community: Optional[int] = None,
        default: bool = False,
    ) -> bool:
        self.calls.append((flag_key, tenant, community))
        return flag_key in self.enabled_flags


class FakeInstallations:
    """`InstallationLookup` returning a fixed row set, or nothing (falls through to default App)."""

    def __init__(self, rows: Optional[List[AppInstallation]] = None) -> None:
        self.rows = rows or []

    async def find(
        self, feature: str, *, tenant: str, community: Optional[int]
    ) -> List[AppInstallation]:
        return [r for r in self.rows if r.feature == feature]


def make_app_registry(
    feature: str = "waddles.bot.shoutout", is_default: bool = True
) -> AppRegistry:
    registry = AppRegistry()
    registry.load(
        {
            "app_id": f"penguintechinc.waddles.{feature.rsplit('.', 1)[-1]}",
            "name": "Default App",
            "version": "1.0.0",
            "feature": feature,
            "module": "bot",
            "provider": "builtin",
            "is_default": is_default,
        }
    )
    return registry


class TestToolDerivation:
    def test_tool_name_bakes_in_version(self) -> None:
        contract = make_contract("bot.shoutout", version=2)
        assert tool_name_for_contract(contract) == "bot.shoutout@2"

    def test_tool_from_contract_wire_shape(self) -> None:
        contract = make_contract("bot.shoutout")
        tool = mcp_tool_from_contract(contract)
        wire = tool.to_dict()
        assert wire["name"] == "bot.shoutout@1"
        assert "bot.shoutout" in wire["description"]
        assert wire["inputSchema"]["type"] == "object"
        assert "community_id" in wire["inputSchema"]["properties"]
        assert "arguments" in wire["inputSchema"]["properties"]

    def test_each_contract_maps_to_exactly_one_tool(self) -> None:
        """No hand-written tool list -- one FeatureContract in, one McpTool out."""
        shoutout = make_contract("bot.shoutout")
        commands = make_contract("bot.commands")
        assert (
            mcp_tool_from_contract(shoutout).name
            != mcp_tool_from_contract(commands).name
        )


class TestListToolsForTenant:
    async def test_free_tenant_sees_only_free_entitled_tools(self) -> None:
        free_tool = make_contract("bot.shoutout", min_tier="free")
        enterprise_tool = make_contract("bot.enterprise_thing", min_tier="enterprise")
        # The fake stands in for the full two-gate (flag AND license) evaluation --
        # only the free-tier flag resolves enabled for this tenant.
        check = FakeCheck(enabled_flags={"waddles.bot.shoutout"})

        tools = await list_tools_for_tenant(
            tenant="free-co", contracts=(free_tool, enterprise_tool), check=check
        )

        names = {t.name for t in tools}
        assert names == {"bot.shoutout@1"}

    async def test_enterprise_tenant_sees_more_tools_and_free_tenant_tool_is_absent_for_enterprise_only(
        self,
    ) -> None:
        free_tool = make_contract("bot.shoutout", min_tier="free")
        enterprise_tool = make_contract("bot.enterprise_thing", min_tier="enterprise")
        check = FakeCheck(
            enabled_flags={"waddles.bot.shoutout", "waddles.bot.enterprise_thing"}
        )

        free_tools = await list_tools_for_tenant(
            tenant="free-co",
            contracts=(free_tool, enterprise_tool),
            check=FakeCheck(enabled_flags={"waddles.bot.shoutout"}),
        )
        enterprise_tools = await list_tools_for_tenant(
            tenant="ent-co", contracts=(free_tool, enterprise_tool), check=check
        )

        assert {t.name for t in enterprise_tools} == {
            "bot.shoutout@1",
            "bot.enterprise_thing@1",
        }
        # The load-bearing assertion: the Enterprise-only tool is ABSENT for the
        # Free tenant -- listing it would leak the product surface (design doc).
        assert "bot.enterprise_thing@1" not in {t.name for t in free_tools}

    async def test_list_tools_for_tenant_excludes_disabled_feature(self) -> None:
        """Same leak-prevention property, isolated to a single tenant/pool -- see
        module docstring for the fail-first proof performed against this test."""
        enabled = make_contract("bot.shoutout")
        disabled = make_contract("bot.raid")
        check = FakeCheck(enabled_flags={"waddles.bot.shoutout"})

        tools = await list_tools_for_tenant(
            tenant="acme", contracts=(enabled, disabled), check=check
        )

        assert {t.name for t in tools} == {"bot.shoutout@1"}


class TestEffectiveScopes:
    def test_maps_role_bundle_to_scopes(self) -> None:
        scopes = effective_scopes(["community:viewer"])
        assert "community:read" in scopes
        assert "community:write" not in scopes

    def test_unrecognized_role_grants_nothing(self) -> None:
        assert effective_scopes(["not-a-real-role"]) == frozenset()

    def test_unions_multiple_roles(self) -> None:
        scopes = effective_scopes(["community:viewer", "tenant:maintainer"])
        assert "community:read" in scopes
        assert "tenant:read" in scopes

    def test_global_admin_wildcard_present(self) -> None:
        assert "*:write" in effective_scopes(["global:admin"])


class TestHasRequiredScopes:
    def test_exact_match(self) -> None:
        assert has_required_scopes(
            frozenset({"bot.command:write"}), frozenset({"bot.command:write"})
        )

    def test_wildcard_resource_satisfies_specific_requirement(self) -> None:
        assert has_required_scopes(
            frozenset({"bot.command:write"}), frozenset({"*:write"})
        )

    def test_wildcard_action_mismatch_does_not_satisfy(self) -> None:
        assert not has_required_scopes(
            frozenset({"bot.command:write"}), frozenset({"*:read"})
        )

    def test_all_required_must_be_covered(self) -> None:
        granted = frozenset({"bot.command:write"})
        required = frozenset({"bot.command:write", "bot.command:admin"})
        assert not has_required_scopes(required, granted)

    def test_empty_requirement_always_satisfied(self) -> None:
        assert has_required_scopes(frozenset(), frozenset())


class TestAuthorizeAndResolveToolCall:
    """The tools/call gate chain: scope -> feature/licensing -> App resolution."""

    def _registry(self) -> FeatureRegistry:
        registry = FeatureRegistry()
        registry.register(
            make_contract(
                "bot.shoutout", requires_scopes=frozenset({"bot.command:write"})
            )
        )
        return registry

    async def test_success_path_returns_contract_and_resolved_app(self) -> None:
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        registry = self._registry()
        app_registry = make_app_registry()
        check = FakeCheck(enabled_flags={"waddles.bot.shoutout"})

        contract, app = await authorize_and_resolve_tool_call(
            "bot.shoutout@1",
            tenant="acme",
            community=None,
            granted_scopes=frozenset({"bot.command:write"}),
            installations=FakeInstallations(),
            registry=registry,
            app_registry=app_registry,
            check=check,
        )

        assert contract.id == "bot.shoutout"
        assert app.app_id == "penguintechinc.waddles.shoutout"
        assert check.calls == [("waddles.bot.shoutout", "acme", None)]

    async def test_unknown_tool_name_shape_rejected(self) -> None:
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "not-a-tool-name",
                tenant="acme",
                community=None,
                granted_scopes=frozenset({"*:write"}),
                installations=FakeInstallations(),
                registry=self._registry(),
            )
        assert excinfo.value.reason == REASON_UNKNOWN_TOOL

    async def test_unregistered_feature_rejected(self) -> None:
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "bot.does_not_exist@1",
                tenant="acme",
                community=None,
                granted_scopes=frozenset({"*:write"}),
                installations=FakeInstallations(),
                registry=self._registry(),
            )
        assert excinfo.value.reason == REASON_UNKNOWN_TOOL

    async def test_stale_version_in_tool_name_rejected(self) -> None:
        """A tool name for a version older/newer than what's registered is unknown, not resolved anyway."""
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "bot.shoutout@2",
                tenant="acme",
                community=None,
                granted_scopes=frozenset({"*:write"}),
                installations=FakeInstallations(),
                registry=self._registry(),
            )
        assert excinfo.value.reason == REASON_UNKNOWN_TOOL

    async def test_denied_when_scope_missing(self) -> None:
        """Fail-first proof performed against this test -- see module docstring."""
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        check = FakeCheck(enabled_flags={"waddles.bot.shoutout"})

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "bot.shoutout@1",
                tenant="acme",
                community=None,
                granted_scopes=frozenset(
                    {"community:read"}
                ),  # missing bot.command:write
                installations=FakeInstallations(),
                registry=self._registry(),
                check=check,
            )

        assert excinfo.value.reason == REASON_SCOPE_DENIED
        # Scope is checked before feature/licensing -- the feature-flag check
        # must never even run for a scope-denied call.
        assert check.calls == []

    async def test_denied_when_feature_disabled_even_with_correct_scope(self) -> None:
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        check = FakeCheck(enabled_flags=set())  # feature off for this tenant

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "bot.shoutout@1",
                tenant="acme",
                community=None,
                granted_scopes=frozenset({"bot.command:write"}),
                installations=FakeInstallations(),
                registry=self._registry(),
                check=check,
            )

        assert excinfo.value.reason == REASON_FEATURE_DISABLED

    async def test_denied_when_no_app_bound(self) -> None:
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        check = FakeCheck(enabled_flags={"waddles.bot.shoutout"})
        empty_app_registry = (
            AppRegistry()
        )  # no App registered at all -- no default, no binding

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "bot.shoutout@1",
                tenant="acme",
                community=None,
                granted_scopes=frozenset({"bot.command:write"}),
                installations=FakeInstallations(),
                registry=self._registry(),
                app_registry=empty_app_registry,
                check=check,
            )

        assert excinfo.value.reason == REASON_NO_APP_BOUND

    async def test_scope_from_one_feature_does_not_grant_a_different_feature(
        self,
    ) -> None:
        """A tool can never widen its own Feature's scopes, and granted scope for
        one Feature must not spill over into authorizing an unrelated one."""
        from flask_core.mcp_server import authorize_and_resolve_tool_call

        registry = FeatureRegistry()
        registry.register(
            make_contract(
                "bot.shoutout", requires_scopes=frozenset({"bot.command:write"})
            )
        )
        registry.register(
            make_contract("bot.raid", requires_scopes=frozenset({"bot.raid:admin"}))
        )
        check = FakeCheck(enabled_flags={"waddles.bot.shoutout", "waddles.bot.raid"})

        with pytest.raises(McpAuthorizationError) as excinfo:
            await authorize_and_resolve_tool_call(
                "bot.raid@1",
                tenant="acme",
                community=None,
                granted_scopes=frozenset(
                    {"bot.command:write"}
                ),  # only shoutout's scope
                installations=FakeInstallations(),
                registry=registry,
                check=check,
            )

        assert excinfo.value.reason == REASON_SCOPE_DENIED
