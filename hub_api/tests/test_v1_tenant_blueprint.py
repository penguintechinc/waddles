"""`blueprints/v1/tenant.py` -- the M2 Core Tenant group (`tenantController.js` port).

Standalone Quart app registering only `tenant_bp`, matching
`test_v1_user_management_blueprint.py`'s pattern.

Fail-first proofs (executed, not narrated):

1. Scope check -- temporarily swapped `require_scope("tenant:admin")` for
   `require_scope("tenant:read")` on `get_tenant`'s decorator chain:
   `test_get_tenant_wrong_scope_is_403` went red (200 instead of 403,
   since the token in `_admin_headers()` no longer carried a scope the
   handler required); reverted, green again.
2. Tenant-mismatch check (this group's security fix over Node's original
   `:tenantSlug`-from-URL design -- see `services/tenant_service.py`'s
   module docstring) -- temporarily commented out the
   `svc.require_matching_tenant(...)` call in `blueprints/v1/tenant.py`'s
   `_tenant_id()`: `test_get_tenant_wrong_slug_is_403` went red (200,
   returning the OTHER tenant's data, instead of 403) -- confirming the
   check is load-bearing, not dead code; reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.tenant import tenant_bp
from services import branding_service
from tests.conftest import TENANT_SLUG, make_user_token

OTHER_TENANT_SLUG = "other-tenant"


@pytest.fixture
def app(tenant_admin_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(tenant_bp)
    quart_app.config["dal"] = tenant_admin_db.dal
    quart_app.config["async_dal"] = tenant_admin_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_second_tenant(tenant_admin_db: Any) -> None:
    tenant_admin_db.dal.tenants.insert(
        slug=OTHER_TENANT_SLUG, display_name="Other Co", is_active=True
    )
    tenant_admin_db.dal.commit()


def _seed_user(tenant_admin_db: Any, *, email: str = "target@example.com") -> int:
    # int(...) -- pydal's sync insert() returns a `Reference` (an int
    # subclass), not a plain `int`. Harmless for internal comparisons, but
    # quart-schema's test client runs EVERY `json=` kwarg through
    # `TypeAdapter(dict).dump_python()` (see `mixins.py::_make_request`),
    # which raises `TypeError: 'None' is not an instance of
    # 'SchemaSerializer'` on a `Reference`-typed value nested in a dict --
    # a new variant of hub_api/PORTING.md's Gotcha #3, found the hard way
    # while writing this test. `dal.<table>.select()` Row.id is a plain
    # `int` (confirmed separately); only the direct return value of
    # `insert()`/`insert_async()` needs this cast before it can safely
    # flow into a request/response payload.
    user_id = tenant_admin_db.dal.hub_users.insert(
        email=email,
        username=email,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    tenant_admin_db.dal.commit()
    return int(user_id)


def _admin_headers(*, user_id: int = 1, tenant: str = TENANT_SLUG) -> dict[str, str]:
    token = make_user_token(user_id=user_id, scope="tenant:admin", tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


class TestScopeEnforcement:
    async def test_get_tenant_no_token_is_401(self, client: Any) -> None:
        response = await client.get(f"/api/v1/tenant/{TENANT_SLUG}")
        assert response.status_code == 401

    async def test_get_tenant_wrong_scope_is_403(self, client: Any, auth_headers: Any) -> None:
        """The representative scope-check: a valid token WITHOUT tenant:admin is refused."""
        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}", headers=auth_headers(scope="platform:read")
        )
        assert response.status_code == 403

    async def test_get_tenant_with_scope_returns_200(self, client: Any) -> None:
        response = await client.get(f"/api/v1/tenant/{TENANT_SLUG}", headers=_admin_headers())
        assert response.status_code == 200


class TestTenantMismatch:
    """Proves the security fix: URL `tenant_slug` must match the caller's own JWT tenant."""

    async def test_get_tenant_wrong_slug_is_403(self, client: Any, tenant_admin_db: Any) -> None:
        _seed_second_tenant(tenant_admin_db)
        # Token issued FOR TENANT_SLUG, but the URL names a different,
        # real, active tenant -- must be refused regardless of scope.
        response = await client.get(
            f"/api/v1/tenant/{OTHER_TENANT_SLUG}", headers=_admin_headers(tenant=TENANT_SLUG)
        )
        assert response.status_code == 403
        body = await response.get_json()
        assert "mismatch" in body["error"]["message"].lower()

    async def test_update_tenant_wrong_slug_is_403(self, client: Any, tenant_admin_db: Any) -> None:
        _seed_second_tenant(tenant_admin_db)
        response = await client.put(
            f"/api/v1/tenant/{OTHER_TENANT_SLUG}",
            headers=_admin_headers(tenant=TENANT_SLUG),
            json={"displayName": "Hijacked"},
        )
        assert response.status_code == 403


class TestGetUpdateTenant:
    async def test_get_tenant_returns_shape(self, client: Any) -> None:
        response = await client.get(f"/api/v1/tenant/{TENANT_SLUG}", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        tenant = body["tenant"]
        assert tenant["slug"] == TENANT_SLUG
        assert tenant["displayName"] == "Acme Corp"
        assert tenant["allowedModuleIds"] is None
        assert tenant["config"] == {}

    async def test_update_tenant_success(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"displayName": "Acme Corp Renamed", "description": "A tenant"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["message"] == "Tenant updated"

        follow_up = await client.get(f"/api/v1/tenant/{TENANT_SLUG}", headers=_admin_headers())
        follow_up_body = await follow_up.get_json()
        assert follow_up_body["tenant"]["displayName"] == "Acme Corp Renamed"
        assert follow_up_body["tenant"]["description"] == "A tenant"

    async def test_update_tenant_no_fields_is_400(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}", headers=_admin_headers(), json={}
        )
        assert response.status_code == 400

    async def test_update_tenant_logo_and_config_only(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Branding (logo/theme) is Professional-tier whitelabel -- see
        # `test_branding_whitelabel.py` for the gate itself; this test is about
        # the update round-trip, so the entitled path is pinned here.
        monkeypatch.setattr(branding_service, "feature_enabled", AsyncMock(return_value=True))
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"logoUrl": "https://example.com/logo.png", "config": {"theme": "dark"}},
        )
        assert response.status_code == 200

        follow_up = await client.get(f"/api/v1/tenant/{TENANT_SLUG}", headers=_admin_headers())
        body = await follow_up.get_json()
        assert body["tenant"]["logoUrl"] == "https://example.com/logo.png"
        assert body["tenant"]["config"] == {"theme": "dark"}


class TestTenantSettings:
    async def test_get_settings_empty(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/settings", headers=_admin_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"] == []

    async def test_update_settings_then_get(self, client: Any) -> None:
        put_response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/settings",
            headers=_admin_headers(),
            json={"settings": [{"key": "theme", "value": "dark"}]},
        )
        assert put_response.status_code == 200

        get_response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/settings", headers=_admin_headers()
        )
        body = await get_response.get_json()
        assert body["settings"] == [{"key": "theme", "value": "dark"}]

        # Upsert: same key, new value.
        await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/settings",
            headers=_admin_headers(),
            json={"settings": [{"key": "theme", "value": "light"}]},
        )
        get_response2 = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/settings", headers=_admin_headers()
        )
        body2 = await get_response2.get_json()
        assert body2["settings"] == [{"key": "theme", "value": "light"}]

    async def test_update_settings_empty_list_is_400(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/settings",
            headers=_admin_headers(),
            json={"settings": []},
        )
        assert response.status_code == 400

    async def test_update_settings_blank_key_is_400(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/settings",
            headers=_admin_headers(),
            json={"settings": [{"key": "   ", "value": "x"}]},
        )
        assert response.status_code == 400


class TestTenantCommunities:
    async def test_get_communities_empty(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/communities", headers=_admin_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["communities"] == []
        assert body["pagination"]["total"] == 0

    async def test_get_communities_scoped_to_tenant(
        self, client: Any, tenant_admin_db: Any
    ) -> None:
        tenant_row = (
            tenant_admin_db.dal(tenant_admin_db.dal.tenants.slug == TENANT_SLUG).select().first()
        )
        _seed_second_tenant(tenant_admin_db)
        other_row = (
            tenant_admin_db.dal(tenant_admin_db.dal.tenants.slug == OTHER_TENANT_SLUG)
            .select()
            .first()
        )
        tenant_admin_db.dal.communities.insert(
            name="mine", display_name="Mine", tenant_id=tenant_row.id, is_active=True
        )
        tenant_admin_db.dal.communities.insert(
            name="theirs", display_name="Theirs", tenant_id=other_row.id, is_active=True
        )
        tenant_admin_db.dal.commit()

        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/communities", headers=_admin_headers()
        )
        body = await response.get_json()
        names = [c["name"] for c in body["communities"]]
        assert names == ["mine"]


class TestTenantModules:
    async def test_get_modules_all_allowed_when_null(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/modules", headers=_admin_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["allModulesAllowed"] is True
        assert body["modules"] == []

    async def test_update_modules_then_get(self, client: Any, tenant_admin_db: Any) -> None:
        # int(...) -- see _seed_user()'s comment: pydal insert() returns a
        # `Reference`, which crashes quart-schema's test-client JSON dump.
        mod_id = int(
            tenant_admin_db.dal.hub_modules.insert(
                name="widgets", display_name="Widgets", is_published=True, category="core"
            )
        )
        tenant_admin_db.dal.commit()

        put_response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/modules",
            headers=_admin_headers(),
            json={"allowedModuleIds": [mod_id]},
        )
        assert put_response.status_code == 200

        get_response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/modules", headers=_admin_headers()
        )
        body = await get_response.get_json()
        assert body["allModulesAllowed"] is False
        assert [m["id"] for m in body["modules"]] == [mod_id]

    async def test_update_modules_empty_list_means_none_allowed(self, client: Any) -> None:
        await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/modules",
            headers=_admin_headers(),
            json={"allowedModuleIds": []},
        )
        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/modules", headers=_admin_headers()
        )
        body = await response.get_json()
        assert body["allModulesAllowed"] is False
        assert body["modules"] == []

    async def test_update_modules_non_positive_id_is_400(self, client: Any) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/modules",
            headers=_admin_headers(),
            json={"allowedModuleIds": [0]},
        )
        assert response.status_code == 400


class TestTenantAdmins:
    async def test_get_admins_empty(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/admins", headers=_admin_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["admins"] == []

    async def test_add_then_list_admin(self, client: Any, tenant_admin_db: Any) -> None:
        user_id = _seed_user(tenant_admin_db, email="newadmin@example.com")

        add_response = await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins",
            headers=_admin_headers(),
            json={"userId": user_id, "role": "tenant-admin"},
        )
        assert add_response.status_code == 200

        list_response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/admins", headers=_admin_headers()
        )
        body = await list_response.get_json()
        assert len(body["admins"]) == 1
        assert body["admins"][0]["userId"] == user_id
        assert body["admins"][0]["role"] == "tenant-admin"

    async def test_add_admin_invalid_role_is_400(self, client: Any, tenant_admin_db: Any) -> None:
        user_id = _seed_user(tenant_admin_db)
        response = await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins",
            headers=_admin_headers(),
            json={"userId": user_id, "role": "not-a-real-role"},
        )
        assert response.status_code == 400

    async def test_add_admin_unknown_user_is_404(self, client: Any) -> None:
        response = await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins",
            headers=_admin_headers(),
            json={"userId": 9999, "role": "tenant-admin"},
        )
        assert response.status_code == 404

    async def test_remove_admin_success(self, client: Any, tenant_admin_db: Any) -> None:
        user_id = _seed_user(tenant_admin_db, email="removeme@example.com")
        await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins",
            headers=_admin_headers(),
            json={"userId": user_id, "role": "tenant-admin"},
        )
        response = await client.delete(
            f"/api/v1/tenant/{TENANT_SLUG}/admins/{user_id}", headers=_admin_headers()
        )
        assert response.status_code == 200

        list_response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/admins", headers=_admin_headers()
        )
        body = await list_response.get_json()
        assert body["admins"] == []

    async def test_add_admin_twice_updates_role(self, client: Any, tenant_admin_db: Any) -> None:
        user_id = _seed_user(tenant_admin_db, email="promote@example.com")
        await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins",
            headers=_admin_headers(),
            json={"userId": user_id, "role": "tenant-admin"},
        )
        second_response = await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins",
            headers=_admin_headers(),
            json={"userId": user_id, "role": "tenant-owner"},
        )
        assert second_response.status_code == 200

        list_response = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/admins", headers=_admin_headers()
        )
        body = await list_response.get_json()
        assert len(body["admins"]) == 1
        assert body["admins"][0]["role"] == "tenant-owner"

    async def test_remove_admin_not_found_is_404(self, client: Any) -> None:
        response = await client.delete(
            f"/api/v1/tenant/{TENANT_SLUG}/admins/9999", headers=_admin_headers()
        )
        assert response.status_code == 404
