"""`blueprints/v1/analytics.py` -- characterization tests for the Analytics module port (M9).

Standalone Quart app registering only `analytics_bp` (PORTING.md test
pattern), `community_db` fixture for tenant/membership-scoped Scenario 2
routes, `AsyncMock`-stubbed `AnalyticsCoreProxyClient.get` (no real network
I/O, same technique `test_event_blueprint.py::proxy_stub` uses against
`EventCalendarProxyClient.request`).

Fail-first proof (executed, not narrated) for the two SECURITY fixes this
module's own docstring documents:

Round 1 -- `community_in_tenant` check: temporarily commented out the
`if not community_in_tenant(...)` early-return in `get_member_stats`,
re-ran `TestCrossTenantLeak::test_community_in_different_tenant_is_404` ->
went red (`assert 200 == 404`, the mocked proxy response leaked through
instead of a 404). Reverted -- green again.

Round 2 -- `community_member_exists` check: temporarily commented out the
`if not svc.community_member_exists(...)` early-return, re-ran
`TestCrossTenantLeak::test_non_member_user_is_404` -> went red
(`assert 200 == 404`). Reverted -- green again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.analytics as analytics_module
from blueprints.v1.analytics import analytics_bp
from services.analytics_proxy import ProxyResult


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(analytics_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def proxy_stub(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the module-level `_proxy_client.get` -- no real network I/O in tests.

    Default return is a successful, empty-body relay wrapped in
    `flask_core.api_utils.success_response`'s own envelope shape
    (`{"success": True, "data": {...}, "timestamp": ...}`), matching what
    `analytics-core` actually returns. Individual tests override
    `.return_value`/`.side_effect`.
    """
    stub = AsyncMock(
        return_value=ProxyResult(ok=True, status_code=200, body={"success": True, "data": {}})
    )
    monkeypatch.setattr(analytics_module._proxy_client, "get", stub)
    return stub


# ---------------------------------------------------------------------------
# Auth bypass -- missing token -> 401 (proves every scope-gated route in
# this group actually runs tenant_middleware, not just require_scope)
# ---------------------------------------------------------------------------


class TestAuthBypass:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/analytics/me/stats",
            "/api/v1/analytics/me/reputation",
            "/api/v1/analytics/platform/overview",
            "/api/v1/analytics/platform/reputation",
            "/api/v1/analytics/platform/growth",
            "/api/v1/analytics/platform/activity",
            "/api/v1/analytics/platform/community-health",
            "/api/v1/analytics/admin/users/1/stats",
            "/api/v1/analytics/admin/users/1/reputation",
        ],
    )
    async def test_missing_token_is_401(self, client: Any, path: str, proxy_stub: Any) -> None:
        response = await client.get(path)
        assert response.status_code == 401

    async def test_community_member_route_missing_token_is_401(
        self, client: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(f"/api/v1/analytics/community/{community_id}/members/7/stats")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 4 -- platform overview: scope gate + `analytics:read` naming
# ---------------------------------------------------------------------------


class TestPlatformScopeGate:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/platform/activity", headers=auth_headers(scope="community:read")
        )
        assert response.status_code == 403

    async def test_analytics_read_scope_is_200(
        self, client: Any, auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/platform/activity",
            headers=auth_headers(scope="analytics:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True

    async def test_global_admin_wildcard_covers_analytics_read(
        self, client: Any, auth_headers: Any, proxy_stub: Any
    ) -> None:
        """Superadmin's `*:read` wildcard satisfies `analytics:read`.

        Matches Node's `isSuperAdmin` bypass in `requireAnalyticsConsumer`.
        """
        response = await client.get(
            "/api/v1/analytics/platform/activity", headers=auth_headers(scope="*:read")
        )
        assert response.status_code == 200

    async def test_growth_forwards_period_query_param(
        self, client: Any, auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/platform/growth?period=30d",
            headers=auth_headers(scope="analytics:read"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/platform/growth", query={"period": "30d"}
        )

    async def test_growth_defaults_period_to_90d(
        self, client: Any, auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/platform/growth", headers=auth_headers(scope="analytics:read")
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/platform/growth", query={"period": "90d"}
        )

    async def test_platform_reputation_relays_proxy_body(
        self, client: Any, auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/platform/reputation",
            headers=auth_headers(scope="analytics:read"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with("/api/v1/analytics/platform/reputation")


# ---------------------------------------------------------------------------
# Scenario 4 -- platform overview reshaping (getPlatformOverview port)
# ---------------------------------------------------------------------------


class TestPlatformOverview:
    async def test_overview_reshapes_three_calls(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_get(path: str, **kwargs: Any) -> ProxyResult:
            if path.endswith("/summary"):
                body = {"success": True, "data": {"total_users": 5, "total_communities": 2}}
            elif path.endswith("/reputation"):
                body = {
                    "success": True,
                    "data": {"stats": {"total": 5}, "histogram": [{"range": "0-50", "count": 5}]},
                }
            else:
                body = {"success": True, "data": {"segments": []}}
            return ProxyResult(ok=True, status_code=200, body=body)

        monkeypatch.setattr(analytics_module._proxy_client, "get", AsyncMock(side_effect=fake_get))
        response = await client.get(
            "/api/v1/analytics/platform/overview", headers=auth_headers(scope="analytics:read")
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "summary": {"total_users": 5, "total_communities": 2},
            "reputationTiers": [{"range": "0-50", "count": 5}],
            "platformBreakdown": [],
            "communityTypes": [],
        }

    async def test_overview_downstream_failure_is_502(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_get(path: str, **kwargs: Any) -> ProxyResult:
            if path.endswith("/activity"):
                return ProxyResult(ok=False, status_code=503, body=None)
            return ProxyResult(ok=True, status_code=200, body={"success": True, "data": {}})

        monkeypatch.setattr(analytics_module._proxy_client, "get", AsyncMock(side_effect=fake_get))
        response = await client.get(
            "/api/v1/analytics/platform/overview", headers=auth_headers(scope="analytics:read")
        )
        assert response.status_code == 502


# ---------------------------------------------------------------------------
# Scenario 4 -- premium feature gate (community-health, Professional tier)
# ---------------------------------------------------------------------------


class TestCommunityHealthFeatureGate:
    async def test_flag_off_is_402(
        self, client: Any, auth_headers: Any, proxy_stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(analytics_module, "feature_enabled", AsyncMock(return_value=False))
        response = await client.get(
            "/api/v1/analytics/platform/community-health",
            headers=auth_headers(scope="analytics:read"),
        )
        assert response.status_code == 402
        proxy_stub.assert_not_awaited()

    async def test_flag_on_is_200(
        self, client: Any, auth_headers: Any, proxy_stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(analytics_module, "feature_enabled", AsyncMock(return_value=True))
        response = await client.get(
            "/api/v1/analytics/platform/community-health",
            headers=auth_headers(scope="analytics:read"),
        )
        assert response.status_code == 200

    async def test_limit_is_clamped(
        self, client: Any, auth_headers: Any, proxy_stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(analytics_module, "feature_enabled", AsyncMock(return_value=True))
        response = await client.get(
            "/api/v1/analytics/platform/community-health?limit=9999",
            headers=auth_headers(scope="analytics:read"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/platform/community-health", query={"limit": "200"}
        )


# ---------------------------------------------------------------------------
# Scenario 1 -- self stats (self-service, no require_scope)
# ---------------------------------------------------------------------------


class TestSelfService:
    async def test_me_stats_uses_own_user_id_no_scope_required(
        self, client: Any, user_auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/me/stats", headers=user_auth_headers(user_id=42)
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/user/42/self", caller_user_id=42, caller_role="user"
        )

    async def test_me_reputation_uses_own_user_id(
        self, client: Any, user_auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/me/reputation", headers=user_auth_headers(user_id=42)
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/user/42/reputation", caller_user_id=42, caller_role="user"
        )


# ---------------------------------------------------------------------------
# Scenario 3 -- superadmin views any user
# ---------------------------------------------------------------------------


class TestSuperAdminUserViews:
    async def test_wrong_scope_is_403(
        self, client: Any, user_auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/admin/users/99/stats",
            headers=user_auth_headers(user_id=1, scope="analytics:read"),
        )
        assert response.status_code == 403

    async def test_users_admin_scope_is_200(
        self, client: Any, user_auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/admin/users/99/stats",
            headers=user_auth_headers(user_id=1, scope="users:admin"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/user/99/self", caller_user_id=1, caller_role="user"
        )

    async def test_admin_reputation_forwards_target_and_caller(
        self, client: Any, user_auth_headers: Any, proxy_stub: Any
    ) -> None:
        response = await client.get(
            "/api/v1/analytics/admin/users/99/reputation",
            headers=user_auth_headers(user_id=1, scope="users:admin"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/user/99/reputation", caller_user_id=1, caller_role="user"
        )


# ---------------------------------------------------------------------------
# Scenario 2 -- community admin views member: tenant + membership scoping
# ---------------------------------------------------------------------------


class TestCommunityMemberViews:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/analytics/community/{community_id}/members/7/stats",
            headers=auth_headers(scope="community.analytics:read"),
        )
        assert response.status_code == 403

    async def test_member_stats_hardcodes_community_admin_caller_role(
        self, client: Any, user_auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        dal, community_id = community_db
        dal.community_members.insert(community_id=community_id, user_id="7", is_active=True)
        dal.commit()
        response = await client.get(
            f"/api/v1/analytics/community/{community_id}/members/7/stats",
            headers=user_auth_headers(user_id=1, scope="community.analytics:admin"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/user/7/in-community/" + str(community_id),
            caller_user_id=1,
            caller_role="community_admin",
        )

    async def test_member_reputation_uses_caller_role_not_hardcoded(
        self, client: Any, user_auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        """Unlike `getUserCommunityStats`, `getUserReputation` derives `caller_role` normally."""
        dal, community_id = community_db
        dal.community_members.insert(community_id=community_id, user_id="7", is_active=True)
        dal.commit()
        response = await client.get(
            f"/api/v1/analytics/community/{community_id}/members/7/reputation",
            headers=user_auth_headers(user_id=1, scope="community.analytics:admin"),
        )
        assert response.status_code == 200
        proxy_stub.assert_awaited_once_with(
            "/api/v1/analytics/user/7/reputation", caller_user_id=1, caller_role="user"
        )


class TestCrossTenantLeak:
    """SECURITY regression -- see this module's own docstring, "Fail-first proof"."""

    async def test_community_in_different_tenant_is_404(
        self, client: Any, user_auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        dal, _own_community_id = community_db
        other_tenant_id = dal.tenants.insert(slug="other-corp", is_active=True)
        other_community_id = dal.communities.insert(
            name="other-tenant-community", tenant_id=other_tenant_id
        )
        dal.commit()

        response = await client.get(
            f"/api/v1/analytics/community/{other_community_id}/members/7/stats",
            headers=user_auth_headers(user_id=1, scope="community.analytics:admin"),
        )
        assert response.status_code == 404
        proxy_stub.assert_not_awaited()

    async def test_non_member_user_is_404(
        self, client: Any, user_auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        _, community_id = community_db
        # No community_members row for user_id="404" -- never joined this community.
        response = await client.get(
            f"/api/v1/analytics/community/{community_id}/members/404/stats",
            headers=user_auth_headers(user_id=1, scope="community.analytics:admin"),
        )
        assert response.status_code == 404
        proxy_stub.assert_not_awaited()

    async def test_non_member_user_is_404_on_reputation_route_too(
        self, client: Any, user_auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/analytics/community/{community_id}/members/404/reputation",
            headers=user_auth_headers(user_id=1, scope="community.analytics:admin"),
        )
        assert response.status_code == 404
        proxy_stub.assert_not_awaited()

    async def test_reputation_route_also_tenant_scoped(
        self, client: Any, user_auth_headers: Any, community_db: Any, proxy_stub: Any
    ) -> None:
        dal, _own_community_id = community_db
        other_tenant_id = dal.tenants.insert(slug="other-corp-2", is_active=True)
        other_community_id = dal.communities.insert(
            name="other-tenant-community-2", tenant_id=other_tenant_id
        )
        dal.commit()

        response = await client.get(
            f"/api/v1/analytics/community/{other_community_id}/members/7/reputation",
            headers=user_auth_headers(user_id=1, scope="community.analytics:admin"),
        )
        assert response.status_code == 404
        proxy_stub.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fail-loud 502 -- upstream failure is never masked as 500 (gh-894)
# ---------------------------------------------------------------------------


class TestMaskedFailure:
    async def test_downstream_failure_is_502(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            analytics_module._proxy_client,
            "get",
            AsyncMock(return_value=ProxyResult(ok=False, status_code=503, body=None)),
        )
        response = await client.get(
            "/api/v1/analytics/platform/activity", headers=auth_headers(scope="analytics:read")
        )
        assert response.status_code == 502
        body = await response.get_json()
        assert body["success"] is False
