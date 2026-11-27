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

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin import superadmin_bp
from services.member_roles import is_owner_role
from services.tenant_key_provisioning import TenantKeyProvisioningError
from tests.community_ids import (
    UNKNOWN_PUBLIC_ID,
    assert_no_numeric_community_ids,
    cpath,
    install_community_ids,
    public_id,
)
from tests.conftest import TENANT_SLUG, make_user_token


class _Runtime:
    """Stand-in envelope runtime; real key provisioning is covered on Postgres elsewhere."""

    service = object()


@pytest.fixture
def provisioned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Replace key provisioning with a recorder (the real one needs the Postgres key store)."""
    calls: list[tuple[int, str]] = []

    async def _fake(dal: Any, service: Any, *, tenant_id: int, slug: str) -> None:
        calls.append((tenant_id, slug))

    monkeypatch.setattr("blueprints.v1.superadmin.provision_new_tenant", _fake)
    return calls


@pytest.fixture
def app(admin_db: Any, provisioned: list[tuple[int, str]]) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, admin_db.dal)
    quart_app.register_blueprint(superadmin_bp)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    quart_app.config["envelope_runtime"] = _Runtime()
    quart_app.config["install_dal"] = object()
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


def _is_linked_owner(dal: Any, member: Any) -> bool:
    """True iff the member links (`community_role_id`) the seeded system owner role row."""
    role_row = dal(dal.community_roles.id == member.community_role_id).select().first()
    return is_owner_role(role_row)


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


def _is_uuid_str(value: Any) -> bool:
    """Whether `value` is a canonical UUID string (community `id` on the superadmin API)."""
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


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
        assert_no_numeric_community_ids(body, expect_at_least=0)
        assert _is_uuid_str(body["communities"][0]["id"])

    async def test_get_community_not_found_is_404(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/superadmin/communities/{UNKNOWN_PUBLIC_ID}", headers=_headers()
        )
        assert response.status_code == 404

    async def test_get_community_found(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.get(
            cpath(admin_db, "/api/v1/superadmin/communities/{community_id}", community_id),
            headers=_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["community"]["id"] == public_id(admin_db, community_id)

    async def test_create_community_success(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "New Community", "platform": "discord"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["community"]["name"] == "new-community"
        assert isinstance(body["community"]["id"], str)
        uuid.UUID(body["community"]["id"])  # public UUID, never the numeric id

    async def test_create_community_stores_canonical_owner_role(
        self, client: Any, admin_db: Any
    ) -> None:
        # regression: the creator must LINK the seeded system owner role (not just carry a string)
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "Owned Community", "platform": "discord"},
        )
        assert response.status_code == 201
        rows = admin_db.dal(admin_db.dal.community_members.id > 0).select()
        assert [r.user_id for r in rows] == ["1"]
        assert _is_linked_owner(admin_db.dal, rows[0])

    async def test_reassign_owner_keeps_canonical_owner_role(
        self, client: Any, admin_db: Any
    ) -> None:
        created = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "Reassign Me", "platform": "discord"},
        )
        created_uuid = (await created.get_json())["community"]["id"]  # public UUID, not numeric
        community_id = int(
            admin_db.dal(admin_db.dal.communities.public_id == created_uuid)
            .select(admin_db.dal.communities.id)
            .first()
            .id
        )
        response = await client.post(
            f"/api/v1/superadmin/communities/{created_uuid}/reassign",
            headers=_headers(),
            json={"newOwnerName": "newowner"},
        )
        assert response.status_code == 200
        rows = admin_db.dal(admin_db.dal.community_members.community_id == community_id).select()
        assert len(rows) == 1
        assert _is_linked_owner(admin_db.dal, rows[0])

    async def test_create_community_missing_platform_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/communities", headers=_headers(), json={"name": "No Platform"}
        )
        assert response.status_code == 400

    async def test_create_community_duplicate_name_is_409(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        tenant_id = dal(dal.tenants.slug == TENANT_SLUG).select().first().id
        _seed_community(admin_db, name="acme-community", tenant_id=tenant_id)
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "Acme Community", "platform": "discord"},
        )
        assert response.status_code == 409

    async def test_create_community_same_name_other_tenant_is_generic_409(
        self, client: Any, admin_db: Any
    ) -> None:
        dal = admin_db.dal
        other = dal.tenants.insert(slug="other-tenant", display_name="Other", is_active=True)
        dal.commit()
        _seed_community(admin_db, name="acme-community", tenant_id=other)
        response = await client.post(
            "/api/v1/superadmin/communities",
            headers=_headers(),
            json={"name": "Acme Community", "platform": "discord"},
        )
        assert response.status_code == 409
        assert "other-tenant" not in (await response.get_data(as_text=True))

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
            cpath(admin_db, "/api/v1/superadmin/communities/{community_id}", community_id),
            headers=_headers(),
            json={"displayName": "Renamed"},
        )
        assert response.status_code == 200

    async def test_update_community_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/superadmin/communities/{UNKNOWN_PUBLIC_ID}",
            headers=_headers(),
            json={"displayName": "X"},
        )
        assert response.status_code == 404

    async def test_delete_community_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.delete(
            cpath(admin_db, "/api/v1/superadmin/communities/{community_id}", community_id),
            headers=_headers(),
        )
        assert response.status_code == 200

    async def test_delete_global_community_is_403(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, is_global=True)
        response = await client.delete(
            cpath(admin_db, "/api/v1/superadmin/communities/{community_id}", community_id),
            headers=_headers(),
        )
        assert response.status_code == 403

    async def test_reassign_owner_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.post(
            cpath(admin_db, "/api/v1/superadmin/communities/{community_id}/reassign", community_id),
            headers=_headers(),
            json={"newOwnerName": "newowner"},
        )
        assert response.status_code == 200

    async def test_reassign_owner_missing_name_is_400(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.post(
            cpath(admin_db, "/api/v1/superadmin/communities/{community_id}/reassign", community_id),
            headers=_headers(),
            json={},
        )
        assert response.status_code == 400


_UNKNOWN_TENANT_UUID = "00000000-0000-4000-8000-0000000000a1"


class TestTenantManagement:
    async def test_list_tenants(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/tenants", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        # admin_db fixture seeds exactly one tenant (TENANT_SLUG)
        assert body["pagination"]["total"] == 1

    async def test_create_tenant_success(self, client: Any, admin_db: Any) -> None:
        # Free-tier cap (1 tenant, catch-all counts): run as the excluded operator-plane
        # `system` tenant -- see tests/test_free_tier_caps.py.
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="system")
        admin_db.dal.commit()
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(tenant="system"),
            json={"slug": "new-tenant", "displayName": "New Tenant"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["tenant"]["slug"] == "new-tenant"

    async def test_create_tenant_provisions_its_key(
        self, client: Any, admin_db: Any, provisioned: list[tuple[int, str]]
    ) -> None:
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="system")
        admin_db.dal.commit()
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(tenant="system"),
            json={"slug": "keyed-tenant", "displayName": "Keyed"},
        )
        body = await response.get_json()
        assert response.status_code == 201
        created = admin_db.dal(admin_db.dal.tenants.slug == "keyed-tenant").select().first()
        assert body["tenant"]["id"] == created.public_id
        assert provisioned == [(created.id, "keyed-tenant")]

    async def test_create_tenant_aborts_when_the_key_cannot_be_provisioned(
        self, client: Any, admin_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(dal: Any, service: Any, *, tenant_id: int, slug: str) -> None:
            raise TenantKeyProvisioningError("kms down")

        monkeypatch.setattr("blueprints.v1.superadmin.provision_new_tenant", _boom)
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="system")
        admin_db.dal.commit()
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(tenant="system"),
            json={"slug": "doomed", "displayName": "Doomed"},
        )
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "TENANT_KEY_UNAVAILABLE"

    async def test_create_tenant_without_envelope_runtime_creates_nothing(
        self, client: Any, app: Quart, admin_db: Any
    ) -> None:
        app.config["envelope_runtime"] = None
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="system")
        admin_db.dal.commit()
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(tenant="system"),
            json={"slug": "never", "displayName": "Never"},
        )
        assert response.status_code == 503
        assert admin_db.dal(admin_db.dal.tenants.slug == "never").select().first() is None

    async def test_create_tenant_invalid_slug_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(),
            json={"slug": "Not Valid!", "displayName": "X"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "slug",
        [
            "verify",
            "resend-verification",
            "privacy-requests",
            "captcha-config",
            "altcha-challenge",
            "preview",
            "public",
            "api",
            "admin",
            "tenant",
            "superadmin",
        ],
    )
    async def test_create_tenant_route_shadowing_slug_is_409(self, client: Any, slug: str) -> None:
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(),
            json={"slug": slug, "displayName": "X"},
        )
        assert response.status_code == 409

    @pytest.mark.parametrize("slug", ["123", "007", " 42 "])
    async def test_create_tenant_digits_only_slug_is_400(self, client: Any, slug: str) -> None:
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(),
            json={"slug": slug, "displayName": "X"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("slug", ["VERIFY", " Public ", "System", "DEFAULT"])
    async def test_reserved_slugs_are_case_insensitive_at_create(
        self, client: Any, slug: str
    ) -> None:
        response = await client.post(
            "/api/v1/superadmin/tenants",
            headers=_headers(),
            json={"slug": slug, "displayName": "X"},
        )
        assert response.status_code in (400, 409)  # never created, whichever rule fires first

    @pytest.mark.parametrize("slug", ["123", " Public ", "SYSTEM", "Bad Slug"])
    async def test_update_tenant_rename_is_validated_and_normalized(
        self, admin_db: Any, slug: str
    ) -> None:
        from services import superadmin_service as svc
        from services.errors import ApiError

        tid = admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).select().first().id
        with pytest.raises(ApiError):
            await svc.update_tenant(admin_db, admin_db.dal, tenant_id=tid, fields={"slug": slug})

    @pytest.mark.parametrize("slug", ["verify", "privacy-requests", "public"])
    async def test_update_tenant_rename_to_route_slug_is_refused(
        self, admin_db: Any, slug: str
    ) -> None:
        from services import superadmin_service as svc
        from services.errors import ApiError

        tenant = admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).select().first()
        with pytest.raises(ApiError) as exc:
            await svc.update_tenant(
                admin_db, admin_db.dal, tenant_id=tenant.id, fields={"slug": slug}
            )
        assert exc.value.status_code == 403

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
            f"/api/v1/superadmin/tenants/{tenant.public_id}",
            headers=_headers(),
            json={"displayName": "Renamed Tenant"},
        )
        assert response.status_code == 200

    async def test_update_tenant_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/superadmin/tenants/{_UNKNOWN_TENANT_UUID}",
            headers=_headers(),
            json={"displayName": "X"},
        )
        assert response.status_code == 404

    async def test_delete_tenant_success(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        new_id = dal.tenants.insert(slug="deletable", display_name="Deletable", is_global=False)
        dal.commit()
        public_id = dal(dal.tenants.id == new_id).select().first().public_id
        response = await client.delete(
            f"/api/v1/superadmin/tenants/{public_id}", headers=_headers()
        )
        assert response.status_code == 200

    async def test_delete_global_tenant_is_403(self, client: Any, admin_db: Any) -> None:
        tenant = admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).select().first()
        admin_db.dal(admin_db.dal.tenants.id == tenant.id).update(is_global=True)
        admin_db.dal.commit()
        response = await client.delete(
            f"/api/v1/superadmin/tenants/{tenant.public_id}", headers=_headers()
        )
        assert response.status_code == 403

    async def test_delete_tenant_not_found_is_404(self, client: Any) -> None:
        response = await client.delete(
            f"/api/v1/superadmin/tenants/{_UNKNOWN_TENANT_UUID}", headers=_headers()
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("bad", ["9999", "1", "not-a-uuid"])
    async def test_numeric_or_malformed_tenant_id_is_404(self, client: Any, bad: str) -> None:
        update = await client.put(
            f"/api/v1/superadmin/tenants/{bad}", headers=_headers(), json={"displayName": "X"}
        )
        delete = await client.delete(f"/api/v1/superadmin/tenants/{bad}", headers=_headers())
        assert (update.status_code, delete.status_code) == (404, 404)
