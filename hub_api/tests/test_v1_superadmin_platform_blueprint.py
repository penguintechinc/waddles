"""`blueprints/v1/superadmin.py` -- the M3 Platform-admin (cross-tenant) group.

Standalone Quart app registering only `superadmin_bp`
(`v1_superadmin_platform` -- distinct from M1's `v1_superadmin_users`),
matching `test_v1_user_management_blueprint.py`'s pattern.

Fail-first proof (executed, not narrated): temporarily swapped
`require_scope("communities:admin")` for `require_scope("communities:read")`
on `list_communities`' decorator chain -- `test_list_communities_wrong_
scope_is_403` went red (200 instead of 403); reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin import superadmin_bp
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
def app(admin_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(superadmin_bp)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers(
    *, user_id: int = 1, scope: str = "*:admin", tenant: str = TENANT_SLUG
) -> dict[str, str]:
    # global:admin bundle's *:admin wildcard -- covers communities:admin,
    # modules:admin, tenants:admin per flask_core.authz._scope_covers.
    token = make_user_token(user_id=user_id, scope=scope, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


def _seed_community(admin_db: Any, **overrides: Any) -> int:
    dal = admin_db.dal
    fields = {
        "name": "acme-community",
        "display_name": "Acme Community",
        "platform": "discord",
        "is_public": True,
        "is_active": True,
        "member_count": 5,
        "config": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fields.update(overrides)
    community_id: int = dal.communities.insert(**fields)
    dal.commit()
    return community_id


def _seed_module(admin_db: Any, **overrides: Any) -> int:
    dal = admin_db.dal
    fields = {
        "name": "polls",
        "display_name": "Polls",
        "is_published": False,
        "is_core": False,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fields.update(overrides)
    module_id: int = dal.hub_modules.insert(**fields)
    dal.commit()
    return module_id


class TestScopeEnforcement:
    async def test_dashboard_no_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/dashboard")
        assert response.status_code == 401

    async def test_list_communities_wrong_scope_is_403(self, client: Any) -> None:
        """The representative scope-check: a valid token WITHOUT *:admin is refused."""
        response = await client.get(
            "/api/v1/superadmin/communities", headers=_headers(scope="communities:read")
        )
        assert response.status_code == 403

    async def test_list_communities_with_scope_returns_200(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/communities", headers=_headers())
        assert response.status_code == 200


class TestDashboard:
    async def test_dashboard_stats(self, client: Any, admin_db: Any) -> None:
        _seed_community(admin_db, platform="discord")
        _seed_community(admin_db, name="beta-community", platform="twitch")
        response = await client.get("/api/v1/superadmin/dashboard", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stats"]["totalCommunities"] == 2
        assert body["stats"]["platformBreakdown"]["discord"] == 1
        assert body["stats"]["platformBreakdown"]["twitch"] == 1
        assert len(body["recentCommunities"]) == 2


class TestCommunityManagement:
    async def test_list_communities_filters_by_search(self, client: Any, admin_db: Any) -> None:
        _seed_community(admin_db, name="acme-community", display_name="Acme")
        _seed_community(admin_db, name="other-community", display_name="Other")
        response = await client.get(
            "/api/v1/superadmin/communities?search=acme", headers=_headers()
        )
        body = await response.get_json()
        assert body["pagination"]["total"] == 1
        assert body["communities"][0]["name"] == "acme-community"

    async def test_get_community_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/communities/9999", headers=_headers())
        assert response.status_code == 404

    async def test_get_community_found(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.get(
            f"/api/v1/superadmin/communities/{community_id}", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["community"]["id"] == community_id

    async def test_create_community_success(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "New Community", "platform": "discord"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["community"]["name"] == "new-community"

    async def test_create_community_missing_platform_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/communities", headers=_headers(), json={"name": "No Platform"}
        )
        assert response.status_code == 400

    async def test_create_community_duplicate_name_is_409(self, client: Any, admin_db: Any) -> None:
        _seed_community(admin_db, name="acme-community")
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "Acme Community", "platform": "discord"},
        )
        assert response.status_code == 409

    async def test_create_community_invalid_type_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "Bad Type", "platform": "discord", "communityType": "not-a-type"},
        )
        assert response.status_code == 400

    async def test_update_community_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/superadmin/communities/{community_id}",
            headers=_headers(),
            json={"displayName": "Renamed"},
        )
        assert response.status_code == 200

    async def test_update_community_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/superadmin/communities/9999",
            headers=_headers(),
            json={"displayName": "X"},
        )
        assert response.status_code == 404

    async def test_delete_community_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.delete(
            f"/api/v1/superadmin/communities/{community_id}", headers=_headers()
        )
        assert response.status_code == 200

    async def test_delete_global_community_is_403(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, is_global=True)
        response = await client.delete(
            f"/api/v1/superadmin/communities/{community_id}", headers=_headers()
        )
        assert response.status_code == 403

    async def test_reassign_owner_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.post(
            f"/api/v1/superadmin/communities/{community_id}/reassign",
            headers=_headers(),
            json={"newOwnerName": "newowner"},
        )
        assert response.status_code == 200

    async def test_reassign_owner_missing_name_is_400(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.post(
            f"/api/v1/superadmin/communities/{community_id}/reassign", headers=_headers(), json={}
        )
        assert response.status_code == 400


class TestMarketplaceModules:
    async def test_get_all_modules_empty(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/marketplace/modules", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["modules"] == []

    async def test_create_module_success(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/marketplace/modules",
            headers=_headers(),
            json={"name": "trivia"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["module"]["name"] == "trivia"
        assert body["module"]["dbAccount"]["provisioned"] is False  # no Postgres function in sqlite

    async def test_create_module_missing_name_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/marketplace/modules", headers=_headers(), json={}
        )
        assert response.status_code == 400

    async def test_create_module_duplicate_name_is_409(self, client: Any, admin_db: Any) -> None:
        _seed_module(admin_db, name="trivia")
        response = await client.post(
            "/api/v1/superadmin/marketplace/modules",
            headers=_headers(),
            json={"name": "trivia"},
        )
        assert response.status_code == 409

    async def test_get_all_modules_with_ratings(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        module_id = _seed_module(admin_db, name="trivia")
        dal.hub_module_reviews.insert(module_id=module_id, rating=4)
        dal.hub_module_reviews.insert(module_id=module_id, rating=5)
        dal.commit()
        response = await client.get("/api/v1/superadmin/marketplace/modules", headers=_headers())
        body = await response.get_json()
        assert body["modules"][0]["avgRating"] == "4.5"
        assert body["modules"][0]["reviewCount"] == 2

    async def test_update_module_success(self, client: Any, admin_db: Any) -> None:
        module_id = _seed_module(admin_db)
        response = await client.put(
            f"/api/v1/superadmin/marketplace/modules/{module_id}",
            headers=_headers(),
            json={"displayName": "Updated"},
        )
        assert response.status_code == 200

    async def test_update_module_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/superadmin/marketplace/modules/9999",
            headers=_headers(),
            json={"displayName": "X"},
        )
        assert response.status_code == 404

    async def test_publish_module_success(self, client: Any, admin_db: Any) -> None:
        module_id = _seed_module(admin_db, is_published=False)
        response = await client.put(
            f"/api/v1/superadmin/marketplace/modules/{module_id}/publish",
            headers=_headers(),
            json={"isPublished": True},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["message"] == "Module published"

    async def test_publish_module_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/superadmin/marketplace/modules/9999/publish",
            headers=_headers(),
            json={"isPublished": True},
        )
        assert response.status_code == 404

    async def test_delete_module_success(self, client: Any, admin_db: Any) -> None:
        module_id = _seed_module(admin_db)
        response = await client.delete(
            f"/api/v1/superadmin/marketplace/modules/{module_id}", headers=_headers()
        )
        assert response.status_code == 200

    async def test_delete_module_blocked_by_installations(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        module_id = _seed_module(admin_db)
        dal.hub_module_installations.insert(community_id=1, module_id=module_id, is_enabled=True)
        dal.commit()
        response = await client.delete(
            f"/api/v1/superadmin/marketplace/modules/{module_id}", headers=_headers()
        )
        assert response.status_code == 400

    async def test_delete_module_not_found_is_404(self, client: Any) -> None:
        response = await client.delete(
            "/api/v1/superadmin/marketplace/modules/9999", headers=_headers()
        )
        assert response.status_code == 404


class TestTenantManagement:
    async def test_list_tenants(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/tenants", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        # admin_db fixture seeds exactly one tenant (TENANT_SLUG)
        assert body["pagination"]["total"] == 1

    async def test_create_tenant_success(self, client: Any, admin_db: Any) -> None:
        # Free-tier cap (1 user-created tenant): the seeded acme-corp counts as it, so
        # run as the reserved catch-all tenant -- see tests/test_free_tier_caps.py.
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="default")
        admin_db.dal.commit()
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(tenant="default"),
            json={"slug": "new-tenant", "displayName": "New Tenant"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["tenant"]["slug"] == "new-tenant"

    async def test_create_tenant_invalid_slug_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(),
            json={"slug": "Not Valid!", "displayName": "X"},
        )
        assert response.status_code == 400

    async def test_create_tenant_duplicate_slug_is_409(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(),
            json={"slug": TENANT_SLUG, "displayName": "Dup"},
        )
        assert response.status_code == 409

    async def test_update_tenant_success(self, client: Any, admin_db: Any) -> None:
        tenant = admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).select().first()
        response = await client.put(
            f"/api/v1/superadmin/tenants/{tenant.id}",
            headers=_headers(),
            json={"displayName": "Renamed Tenant"},
        )
        assert response.status_code == 200

    async def test_update_tenant_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/superadmin/tenants/9999", headers=_headers(), json={"displayName": "X"}
        )
        assert response.status_code == 404

    async def test_delete_tenant_success(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        new_id = dal.tenants.insert(slug="deletable", display_name="Deletable", is_global=False)
        dal.commit()
        response = await client.delete(f"/api/v1/superadmin/tenants/{new_id}", headers=_headers())
        assert response.status_code == 200

    async def test_delete_global_tenant_is_403(self, client: Any, admin_db: Any) -> None:
        tenant = admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).select().first()
        admin_db.dal(admin_db.dal.tenants.id == tenant.id).update(is_global=True)
        admin_db.dal.commit()
        response = await client.delete(
            f"/api/v1/superadmin/tenants/{tenant.id}", headers=_headers()
        )
        assert response.status_code == 403

    async def test_delete_tenant_not_found_is_404(self, client: Any) -> None:
        response = await client.delete("/api/v1/superadmin/tenants/9999", headers=_headers())
        assert response.status_code == 404
