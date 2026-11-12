"""`blueprints/v1/admin.py` -- the M3 Platform-admin (community-scoped) group.

Standalone Quart app registering only `admin_bp`, matching
`test_v1_user_management_blueprint.py`'s pattern.

Fail-first proofs (executed, not narrated):
1. Scope check: temporarily swapped `require_scope("community:admin")` for
   `require_scope("community:owner_only")` on `get_members`'s decorator
   chain -- `test_get_members_wrong_scope_is_403` went red (200 instead of
   403); reverted, green again.
2. Tenant-isolation (IDOR) check: temporarily made `_require_community()`
   in `services/admin_service.py` a no-op (`return`) -- `test_
   cross_tenant_community_is_404` went red (200 with the other tenant's
   member list instead of 404); reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.admin import admin_bp
from services import community_reputation_service as reputation_svc
from tests.conftest import TENANT_SLUG, make_user_token

OTHER_TENANT_SLUG = "other-tenant"


@pytest.fixture
def app(admin_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(admin_bp)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers(
    *, user_id: int = 1, scope: str = "community:admin", tenant: str = TENANT_SLUG
) -> dict[str, str]:
    token = make_user_token(user_id=user_id, scope=scope, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


def _seed_community(admin_db: Any, *, tenant_slug: str = TENANT_SLUG, **overrides: Any) -> int:
    dal = admin_db.dal
    tenant = dal(dal.tenants.slug == tenant_slug).select().first()
    if tenant is None:
        tenant_id = dal.tenants.insert(slug=tenant_slug, display_name=tenant_slug, is_active=True)
    else:
        tenant_id = tenant.id
    fields = {
        "name": "acme-community",
        "display_name": "Acme Community",
        "tenant_id": tenant_id,
        "platform": "discord",
        "is_public": True,
        "join_mode": "open",
        "member_count": 1,
        "is_active": True,
        "config": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fields.update(overrides)
    community_id: int = dal.communities.insert(**fields)
    dal.commit()
    return community_id


def _seed_member(
    admin_db: Any, *, community_id: int, user_id: int, role: str = "member", **overrides: Any
) -> int:
    dal = admin_db.dal
    fields = {
        "community_id": community_id,
        "user_id": str(user_id),
        "user_uuid": f"00000000-0000-4000-8000-{user_id:012d}",
        "role": role,
        "reputation": 600,
        "is_active": True,
        "joined_at": datetime.now(UTC),
    }
    fields.update(overrides)
    member_id: int = dal.community_members.insert(**fields)
    dal.hub_users.insert(
        id=user_id, email=f"user{user_id}@example.com", username=f"user{user_id}", is_active=True
    ) if not dal(dal.hub_users.id == user_id).select().first() else None
    dal.commit()
    return member_id


@pytest.fixture(autouse=True)
def _fake_reputation_apply(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Python stand-in for the Postgres-only `reputation_apply()` (route-contract tests only).

    Mirrors its clamp (300-850, start 600). The real function -- row lock, clamp, idempotency,
    concurrency, tenant isolation -- is exercised against real Postgres in
    `alembic/tests/test_0096_reputation_unify.py`.
    """
    balances: dict[tuple[int, str], int] = {}
    calls: list[dict[str, Any]] = []

    async def fake(async_dal: Any, **kw: Any) -> reputation_svc.ApplyResult:
        calls.append(kw)
        key = (kw["community_id"], kw["user_uuid"])
        new = max(300, min(850, balances.get(key, 600) + kw["delta"]))
        balances[key] = new
        return reputation_svc.ApplyResult(new_score=new, applied=True)

    monkeypatch.setattr(reputation_svc, "apply_reputation", fake)
    return calls


class TestScopeAndTenantEnforcement:
    async def test_get_members_no_token_is_401(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.get(f"/api/v1/admin/{community_id}/members")
        assert response.status_code == 401

    async def test_get_members_wrong_scope_is_403(self, client: Any, admin_db: Any) -> None:
        """The representative scope-check: a valid token WITHOUT community:admin is refused."""
        community_id = _seed_community(admin_db)
        response = await client.get(
            f"/api/v1/admin/{community_id}/members", headers=_headers(scope="community:read")
        )
        assert response.status_code == 403

    async def test_cross_tenant_community_is_404(self, client: Any, admin_db: Any) -> None:
        """The representative IDOR/tenant-isolation check."""
        other_community_id = _seed_community(admin_db, tenant_slug=OTHER_TENANT_SLUG)
        response = await client.get(
            f"/api/v1/admin/{other_community_id}/members", headers=_headers()
        )
        assert response.status_code == 404


class TestCommunitySettings:
    async def test_get_settings(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, description="hi")
        response = await client.get(f"/api/v1/admin/{community_id}/settings", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"]["description"] == "hi"
        assert body["settings"]["joinMode"] == "open"

    async def test_update_settings(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/settings",
            headers=_headers(),
            json={"displayName": "New Name", "joinMode": "approval"},
        )
        assert response.status_code == 200
        follow_up = await client.get(f"/api/v1/admin/{community_id}/settings", headers=_headers())
        body = await follow_up.get_json()
        assert body["settings"]["displayName"] == "New Name"
        assert body["settings"]["joinMode"] == "approval"

    async def test_update_settings_invalid_join_mode_is_400(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/settings",
            headers=_headers(),
            json={"joinMode": "not-a-real-mode"},
        )
        assert response.status_code == 400

    async def test_update_settings_no_fields_is_400(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/settings", headers=_headers(), json={}
        )
        assert response.status_code == 400


class TestMembers:
    async def test_get_members_lists_active(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        response = await client.get(f"/api/v1/admin/{community_id}/members", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["total"] == 1
        assert body["members"][0]["userId"] == "42"
        assert body["members"][0]["reputation"]["score"] == 600

    async def test_update_member_role_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/role",
            headers=_headers(),
            json={"role": "moderator"},
        )
        assert response.status_code == 200

    async def test_update_member_role_invalid_role_is_400(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/role",
            headers=_headers(),
            json={"role": "not-a-role"},
        )
        assert response.status_code == 400

    async def test_update_member_role_cannot_change_owner(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="community-owner")
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/role",
            headers=_headers(),
            json={"role": "member"},
        )
        assert response.status_code == 403

    async def test_update_member_role_promote_to_admin_requires_owner(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        # Caller (user_id=1) is not a member of this community at all --
        # cannot promote someone to community-admin.
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/role",
            headers=_headers(user_id=1),
            json={"role": "community-admin"},
        )
        assert response.status_code == 403

    async def test_update_member_role_not_found_is_404(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/9999/role",
            headers=_headers(),
            json={"role": "moderator"},
        )
        assert response.status_code == 404

    async def test_adjust_reputation_relative(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, reputation=600)
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/reputation",
            headers=_headers(),
            json={"amount": 50, "reason": "helpful"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["reputation"]["score"] == 650
        assert body["change"] == 50
        assert set(body) == {"success", "reputation", "change"}  # exact top-level field set
        assert set(body["reputation"]) == {
            "score",
            "label",
            "shortLabel",
            "tierMin",
            "tierMax",
            "systemMin",
            "systemMax",
        }

    async def test_adjust_reputation_set_to(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, reputation=600)
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/reputation",
            headers=_headers(),
            json={"setTo": 900},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["reputation"]["score"] == 850  # clamped to REPUTATION_MAX

    async def test_adjust_reputation_invalid_amount_is_400(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, reputation=600)
        response = await client.put(
            f"/api/v1/admin/{community_id}/members/42/reputation",
            headers=_headers(),
            json={"amount": 0},
        )
        assert response.status_code == 400

    async def test_remove_member_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, member_count=2)
        _seed_member(admin_db, community_id=community_id, user_id=42)
        response = await client.delete(
            f"/api/v1/admin/{community_id}/members/42", headers=_headers(), json={"reason": "spam"}
        )
        assert response.status_code == 200

    async def test_remove_member_cannot_remove_owner(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="community-owner")
        response = await client.delete(
            f"/api/v1/admin/{community_id}/members/42", headers=_headers(), json={}
        )
        assert response.status_code == 403

    async def test_remove_member_not_found_is_404(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.delete(
            f"/api/v1/admin/{community_id}/members/9999", headers=_headers(), json={}
        )
        assert response.status_code == 404


class TestTempPassword:
    async def test_generate_temp_password_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.post(
            f"/api/v1/admin/{community_id}/temp-password",
            headers=_headers(),
            json={"userIdentifier": "some-platform-user"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert "tempPassword" in body
        assert "-" in body["tempPassword"]


class TestModules:
    async def test_get_modules_empty(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.get(f"/api/v1/admin/{community_id}/modules", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["modules"] == []

    async def test_get_modules_with_installation(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        community_id = _seed_community(admin_db)
        mod_id = dal.modules.insert(name="polls", display_name="Polls", is_active=True)
        dal.module_installations.insert(
            community_id=community_id, module_id=str(mod_id), is_enabled=True, config={}
        )
        dal.commit()
        response = await client.get(f"/api/v1/admin/{community_id}/modules", headers=_headers())
        body = await response.get_json()
        found = body["modules"][0]
        assert found["name"] == "Polls" or found["moduleId"] == str(mod_id)

    async def test_update_module_config_success(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        community_id = _seed_community(admin_db)
        mod_id = dal.modules.insert(name="polls", display_name="Polls", is_active=True)
        dal.module_installations.insert(
            community_id=community_id, module_id=str(mod_id), is_enabled=True, config={}
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/{community_id}/modules/{mod_id}/config",
            headers=_headers(),
            json={"isEnabled": False},
        )
        assert response.status_code == 200

    async def test_update_module_config_not_found_is_404(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/modules/9999/config",
            headers=_headers(),
            json={"isEnabled": False},
        )
        assert response.status_code == 404

    async def test_update_module_config_no_fields_is_400(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        community_id = _seed_community(admin_db)
        mod_id = dal.modules.insert(name="polls", is_active=True)
        dal.module_installations.insert(
            community_id=community_id, module_id=str(mod_id), is_enabled=True
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/{community_id}/modules/{mod_id}/config", headers=_headers(), json={}
        )
        assert response.status_code == 400

    async def test_update_module_config_cannot_disable_core(
        self, client: Any, admin_db: Any
    ) -> None:
        dal = admin_db.dal
        community_id = _seed_community(admin_db)
        hub_mod_id = dal.hub_modules.insert(name="identity", is_core=True)
        dal.module_installations.insert(
            community_id=community_id, module_id=str(hub_mod_id), is_enabled=True
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/{community_id}/modules/{hub_mod_id}/config",
            headers=_headers(),
            json={"isEnabled": False},
        )
        assert response.status_code == 403


class TestConnectedPlatformsAndCommands:
    async def test_get_connected_platforms(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        community_id = _seed_community(admin_db)
        dal.community_servers.insert(
            community_id=community_id,
            platform="discord",
            platform_server_id="123",
            status="approved",
        )
        dal.commit()
        response = await client.get(
            f"/api/v1/admin/{community_id}/connected-platforms", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["connectedPlatforms"][0]["platform"] == "discord"
        assert body["connectedPlatforms"][0]["isActive"] is True

    async def test_get_commands(self, client: Any, admin_db: Any) -> None:
        dal = admin_db.dal
        community_id = _seed_community(admin_db)
        dal.commands.insert(command="!help", module_name="core", community_id=None, is_active=True)
        dal.commit()
        response = await client.get(f"/api/v1/admin/{community_id}/commands", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert "success" not in body  # byte-faithful to Node: no success wrapper
        assert body["commands"][0]["command"] == "!help"
        assert body["commands"][0]["is_enabled"] is True
