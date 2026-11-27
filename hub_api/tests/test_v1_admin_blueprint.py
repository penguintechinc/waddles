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

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.admin import admin_bp
from services import community_reputation_service as reputation_svc
from services.membership_state import MembershipState
from tests.community_ids import install_community_ids, public_id
from tests.conftest import TENANT_SLUG, make_user_token
from tests.user_ids import uid

OTHER_TENANT_SLUG = "other-tenant"


@pytest.fixture
def app(admin_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    install_community_ids(quart_app, admin_db.dal)
    quart_app.register_blueprint(admin_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers(
    *, user_id: int = 1, scope: str = "community:admin users:admin", tenant: str = TENANT_SLUG
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


def _seed_admin_actor(admin_db: Any, user_id: int = 1) -> None:
    """The acting admin's `hub_users` row (its UUID is the ledger actor for reputation writes)."""
    dal = admin_db.dal
    if not dal(dal.hub_users.id == user_id).select().first():
        dal.hub_users.insert(
            id=user_id,
            email=f"user{user_id}@example.com",
            username=f"user{user_id}",
            is_active=True,
        )
        dal.commit()


def _seed_linked(admin_db: Any, *, community_id: int, user_id: int, role: str) -> int:
    """Seed (once) the SYSTEM role row ``role`` and an active member LINKED to it; return its id.

    The seeded admin/owner names get the admin kind (full computed scopes), others fixed.
    """
    dal = admin_db.dal
    row = (
        dal((dal.community_roles.community_id == community_id) & (dal.community_roles.name == role))
        .select()
        .first()
    )
    if row is None:
        role_id = int(
            dal.community_roles.insert(
                community_id=community_id,
                name=role,
                is_system=True,
                role_kind=1 if role in ("community-owner", "community-admin") else 3,
                base_claims={"scopes": []},
            )
        )
    else:
        role_id = int(row.id)
    _seed_member(
        admin_db, community_id=community_id, user_id=user_id, role=role, community_role_id=role_id
    )
    return role_id


def _role_json(admin_db: Any, community_id: int, name: str) -> dict[str, str]:
    """Request body assigning the community's role row ``name`` (by its public UUID)."""
    dal = admin_db.dal
    row = (
        dal((dal.community_roles.community_id == community_id) & (dal.community_roles.name == name))
        .select()
        .first()
    )
    return {"roleId": str(row.public_id)}


#: A community member's token: no `users:admin`, so no global-admin bypass.
_MEMBER_SCOPE = "community:admin"

#: The owner (42) and an admin (7) the owner appointed, both linked to their system role rows.
OWNER, APPOINTEE = 42, 7


def _seed_owner_and_appointee(admin_db: Any, community_id: int) -> None:
    _seed_linked(admin_db, community_id=community_id, user_id=OWNER, role="community-owner")
    _seed_linked(admin_db, community_id=community_id, user_id=APPOINTEE, role="community-admin")


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
    `alembic/tests/test_0097_reputation_unify.py`.
    """
    balances: dict[tuple[int, str], int] = {}
    calls: list[dict[str, Any]] = []

    async def fake(async_dal: Any, **kw: Any) -> reputation_svc.ApplyResult:
        calls.append(kw)
        key = (kw["community_id"], kw["user_uuid"])
        new = max(300, min(850, balances.get(key, 600) + kw["delta"]))
        balances[key] = new
        return reputation_svc.ApplyResult(new_score=new, applied=True)

    def fake_sync(dal: Any, **kw: Any) -> reputation_svc.ApplyResult:
        calls.append(kw)
        key = (kw["community_id"], kw["user_uuid"])
        new = max(300, min(850, balances.get(key, 600) + kw["delta"]))
        balances[key] = new
        return reputation_svc.ApplyResult(new_score=new, applied=True)

    monkeypatch.setattr(reputation_svc, "apply_reputation", fake)
    # The admin override applies inside its locked transaction through the sync twin.
    monkeypatch.setattr(reputation_svc, "apply_reputation_sync", fake_sync)
    return calls


class TestScopeAndTenantEnforcement:
    async def test_get_members_no_token_is_401(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.get(f"/api/v1/admin/{public_id(admin_db, community_id)}/members")
        assert response.status_code == 401

    async def test_get_members_wrong_scope_is_403(self, client: Any, admin_db: Any) -> None:
        """The representative scope-check: a valid token WITHOUT community:admin is refused."""
        community_id = _seed_community(admin_db)
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members",
            headers=_headers(scope="community:read"),
        )
        assert response.status_code == 403

    async def test_cross_tenant_community_is_404(self, client: Any, admin_db: Any) -> None:
        """The representative IDOR/tenant-isolation check."""
        other_community_id = _seed_community(admin_db, tenant_slug=OTHER_TENANT_SLUG)
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, other_community_id)}/members", headers=_headers()
        )
        assert response.status_code == 404


class TestCommunitySettings:
    async def test_get_settings(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, description="hi")
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/settings", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"]["description"] == "hi"
        assert body["settings"]["joinMode"] == "open"

    async def test_update_settings(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/settings",
            headers=_headers(),
            json={"displayName": "New Name", "joinMode": "approval"},
        )
        assert response.status_code == 200
        follow_up = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/settings", headers=_headers()
        )
        body = await follow_up.get_json()
        assert body["settings"]["displayName"] == "New Name"
        assert body["settings"]["joinMode"] == "approval"

    async def test_update_settings_invalid_join_mode_is_400(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/settings",
            headers=_headers(),
            json={"joinMode": "not-a-real-mode"},
        )
        assert response.status_code == 400

    async def test_update_settings_no_fields_is_400(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/settings",
            headers=_headers(),
            json={},
        )
        assert response.status_code == 400


class TestMembers:
    async def test_get_members_lists_active(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["total"] == 1
        assert body["members"][0]["userId"] == uid(42)
        assert body["members"][0]["reputation"]["score"] == 600

    async def test_get_members_exact_shape_and_role(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        role = self._seed_role(admin_db, community_id, "vip", 0, [])
        _seed_member(admin_db, community_id=community_id, user_id=42, community_role_id=role)
        _seed_member(admin_db, community_id=community_id, user_id=43)
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members", headers=_headers()
        )
        body = await response.get_json()
        assert set(body) == {"success", "members", "pagination"}
        assert set(body["pagination"]) == {"page", "limit", "total", "totalPages"}
        by_user = {m["userId"]: m for m in body["members"]}
        assert set(by_user[uid(42)]) == {
            "userId",
            "username",
            "avatarUrl",
            "roleId",
            "roleName",
            "reputation",
            "joinedAt",
        }
        assert by_user[uid(42)]["roleId"] == self._rid(admin_db, role)
        assert by_user[uid(42)]["roleName"] == "vip"
        assert by_user[uid(43)]["roleId"] is None
        assert by_user[uid(43)]["roleName"] is None

    async def test_get_members_role_filter(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        role = self._seed_role(admin_db, community_id, "vip", 0, [])
        _seed_member(admin_db, community_id=community_id, user_id=42, community_role_id=role)
        _seed_member(admin_db, community_id=community_id, user_id=43)
        base = f"/api/v1/admin/{public_id(admin_db, community_id)}/members"
        hit = await (
            await client.get(f"{base}?roleId={self._rid(admin_db, role)}", headers=_headers())
        ).get_json()
        assert [m["userId"] for m in hit["members"]] == [uid(42)]
        assert hit["pagination"]["total"] == 1
        miss = await (
            await client.get(f"{base}?roleId={uuid.uuid4()}", headers=_headers())
        ).get_json()
        assert miss["members"] == [] and miss["pagination"]["total"] == 0
        bad = await client.get(f"{base}?roleId=nope", headers=_headers())
        assert bad.status_code == 400

    async def test_get_members_search_applies_before_pagination(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        for n in range(40, 45):
            _seed_member(admin_db, community_id=community_id, user_id=n)
        _seed_member(admin_db, community_id=community_id, user_id=7)
        base = f"/api/v1/admin/{public_id(admin_db, community_id)}/members"
        body = await (
            await client.get(f"{base}?search=user4&limit=2&page=2", headers=_headers())
        ).get_json()
        assert body["pagination"] == {"page": 2, "limit": 2, "total": 5, "totalPages": 3}
        assert len(body["members"]) == 2
        one = await (await client.get(f"{base}?search=user43", headers=_headers())).get_json()
        assert one["pagination"]["total"] == 1
        assert one["members"][0]["userId"] == uid(43)

    async def test_get_members_search_escapes_like_metacharacters(
        self, client: Any, admin_db: Any
    ) -> None:
        """Regression (PR #1542 review): `%` and `_` in `search` match literally."""
        community_id = _seed_community(admin_db)
        for user_id in (61, 62):
            _seed_member(admin_db, community_id=community_id, user_id=user_id)
        base = f"/api/v1/admin/{public_id(admin_db, community_id)}/members"
        examined = 0
        for term in ("%", "_", "user_", "%6%", "\\"):
            response = await client.get(f"{base}?search={quote(term)}", headers=_headers())
            assert response.status_code == 200
            assert (await response.get_json())["members"] == [], term
            examined += 1
        assert examined == 5
        control = await (await client.get(f"{base}?search=user6", headers=_headers())).get_json()
        assert len(control["members"]) == 2

    async def test_get_members_excludes_banned(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42)
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=43,
            membership_state=MembershipState.BANNED,
        )
        body = await (
            await client.get(
                f"/api/v1/admin/{public_id(admin_db, community_id)}/members", headers=_headers()
            )
        ).get_json()
        assert [m["userId"] for m in body["members"]] == [uid(42)]
        assert body["pagination"]["total"] == 1

    async def test_get_roles_gated_and_shaped(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        vip = self._seed_role(admin_db, community_id, "vip", 0, [])
        admin_db.dal.community_roles[admin_role].update_record(priority=100)
        admin_db.dal.community_roles[vip].update_record(priority=10)
        _seed_member(admin_db, community_id=community_id, user_id=42, community_role_id=admin_role)
        _seed_member(admin_db, community_id=community_id, user_id=43, community_role_id=vip)
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=44,
            community_role_id=vip,
            membership_state=MembershipState.BANNED,
        )
        admin_db.dal.commit()
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/roles"
        denied = await client.get(url, headers=_headers(user_id=9, scope="community:read"))
        assert denied.status_code == 403
        body = await (await client.get(url, headers=_headers())).get_json()
        assert set(body) == {"success", "roles"}
        assert [r["name"] for r in body["roles"]] == ["community-admin", "vip"]
        assert set(body["roles"][0]) == {
            "id",
            "name",
            "displayName",
            "roleKind",
            "priority",
            "memberCount",
        }
        assert [r["memberCount"] for r in body["roles"]] == [1, 1]
        assert body["roles"][0]["id"] == self._rid(admin_db, admin_role)

    async def test_get_roles_tenant_mismatch_is_404(self, client: Any, admin_db: Any) -> None:
        other = _seed_community(admin_db, tenant_slug=OTHER_TENANT_SLUG)
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, other)}/roles", headers=_headers()
        )
        assert response.status_code in (403, 404)

    async def test_update_member_role_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        role = self._seed_role(
            admin_db, community_id, "moderator", 2, ["community:manage_channels"]
        )
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(),
            json={"roleId": self._rid(admin_db, role)},
        )
        assert response.status_code == 200
        row = admin_db.dal(admin_db.dal.community_members.user_id == "42").select().first()
        assert row.community_role_id == role

    async def test_update_member_role_unknown_role_fails_closed(
        self, client: Any, admin_db: Any
    ) -> None:
        """An unknown roleId (or one from another community) is refused, never assigned."""
        community_id = _seed_community(admin_db)
        other = _seed_community(admin_db, name="other-community")
        foreign = self._seed_role(admin_db, other, "moderator", 2, [])
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role"
        for rid in (str(uuid.uuid4()), self._rid(admin_db, foreign)):
            response = await client.put(url, headers=_headers(), json={"roleId": rid})
            assert response.status_code == 400
        row = admin_db.dal(admin_db.dal.community_members.user_id == "42").select().first()
        assert row.community_role_id is None

    async def test_update_member_role_malformed_role_id_is_400(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(),
            json={"roleId": "not-a-uuid"},
        )
        assert response.status_code == 400

    async def test_update_member_role_protects_last_admin(self, client: Any, admin_db: Any) -> None:
        """The last active admin-kind member cannot be moved off the admin-kind role (409)."""
        community_id = _seed_community(admin_db)
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        vip = self._seed_role(admin_db, community_id, "vip", 0, ["community:read"])
        _seed_member(admin_db, community_id=community_id, user_id=42, community_role_id=admin_role)
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role"
        demote = {"roleId": self._rid(admin_db, vip)}
        assert (await client.put(url, headers=_headers(), json=demote)).status_code == 409
        # A second active admin lifts the protection.
        _seed_member(admin_db, community_id=community_id, user_id=43, community_role_id=admin_role)
        assert (await client.put(url, headers=_headers(), json=demote)).status_code == 200

    async def test_last_admin_ignores_banned_admins(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        vip = self._seed_role(admin_db, community_id, "vip", 0, ["community:read"])
        _seed_member(admin_db, community_id=community_id, user_id=42, community_role_id=admin_role)
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=43,
            community_role_id=admin_role,
            membership_state=MembershipState.BANNED,
        )
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(),
            json={"roleId": self._rid(admin_db, vip)},
        )
        assert response.status_code == 409

    async def test_update_member_role_without_scope_is_403(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        role = self._seed_role(admin_db, community_id, "vip", 0, [])
        _seed_member(admin_db, community_id=community_id, user_id=42)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(user_id=9, scope="community:read"),
            json={"roleId": self._rid(admin_db, role)},
        )
        assert response.status_code == 403

    async def test_update_member_role_cannot_change_owner(self, client: Any, admin_db: Any) -> None:
        # Regression (PR #1539 review): an admin the creator appointed holds every scope, yet can
        # never demote the owner -- decided by the LINKED system owner role, not the role string.
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=43, role="member")
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(OWNER)}/role",
            headers=_headers(user_id=APPOINTEE, scope=_MEMBER_SCOPE),
            json=_role_json(admin_db, community_id, "member"),
        )
        assert response.status_code == 403
        dal = admin_db.dal
        owner = dal(dal.community_members.user_id == str(OWNER)).select().first()
        assert owner.role == "community-owner"
        # control: the same caller CAN change a member it out-scopes (not a blanket deny)
        ok = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(43)}/role",
            headers=_headers(user_id=APPOINTEE, scope=_MEMBER_SCOPE),
            json=_role_json(admin_db, community_id, "member"),
        )
        assert ok.status_code == 200

    async def test_owner_role_is_never_assignable_by_role_change(
        self, client: Any, admin_db: Any
    ) -> None:
        """Regression (PR #1542 review, HIGH): PUT role with the OWNER role UUID is 403.

        Ownership moves only through the explicit ownership transfer -- not even a global admin
        (bypass) may mint a second owner through the role route.
        """
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=43, role="member")
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(43)}/role"
        owner_role = _role_json(admin_db, community_id, "community-owner")
        for headers in (_headers(user_id=OWNER, scope=_MEMBER_SCOPE), _headers()):
            response = await client.put(url, headers=headers, json=owner_role)
            assert response.status_code == 403
        dal = admin_db.dal
        row = dal(dal.community_members.user_id == "43").select().first()
        assert row.role == "member"

    async def test_only_assignable_system_roles_or_own_custom_roles(
        self, client: Any, admin_db: Any
    ) -> None:
        """A non-assignable seeded role (`speaker`) is refused; an own custom role is not."""
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=43, role="member")
        speaker = self._seed_role(admin_db, community_id, "speaker", 3, [])
        dal = admin_db.dal
        custom = int(
            dal.community_roles.insert(
                community_id=community_id,
                name="event-helper",
                is_system=False,
                role_kind=0,
                base_claims={"scopes": ["community:read"]},
            )
        )
        dal.commit()
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(43)}/role"
        refused = await client.put(
            url, headers=_headers(), json={"roleId": self._rid(admin_db, speaker)}
        )
        assert refused.status_code == 403
        assert dal(dal.community_members.user_id == "43").select().first().role == "member"
        ok = await client.put(url, headers=_headers(), json={"roleId": self._rid(admin_db, custom)})
        assert ok.status_code == 200
        row = dal(dal.community_members.user_id == "43").select().first()
        assert row.community_role_id == custom

    async def test_global_admin_may_change_owner(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=43, role="member")
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(OWNER)}/role",
            headers=_headers(),  # `users:admin` = global admin bypass
            json=_role_json(admin_db, community_id, "member"),
        )
        assert response.status_code == 200

    async def test_moderator_cannot_change_admin_it_does_not_outscope(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=43, role="member")
        mod_role = self._seed_role(
            admin_db, community_id, "moderator", 2, ["members:roles.manage", "community:read"]
        )
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=9,
            role="moderator",
            community_role_id=mod_role,
        )
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(APPOINTEE)}/role",
            headers=_headers(user_id=9, scope=_MEMBER_SCOPE),
            json=_role_json(admin_db, community_id, "member"),
        )
        assert response.status_code == 403

    async def test_unlinked_owner_string_grants_nothing(self, client: Any, admin_db: Any) -> None:
        # A legacy row whose role STRING says owner but links no role row holds no scopes, so it
        # cannot pass the route's scope gate as a caller.
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=50, role="community-owner")
        _seed_linked(admin_db, community_id=community_id, user_id=51, role="member")
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(51)}/role",
            headers=_headers(user_id=50, scope="community:read"),
            json=_role_json(admin_db, community_id, "member"),
        )
        assert response.status_code == 403

    @staticmethod
    def _rid(admin_db: Any, role_id: int) -> str:
        return str(admin_db.dal.community_roles[role_id].public_id)

    @staticmethod
    def _seed_role(
        admin_db: Any, community_id: int, name: str, kind: int, scopes: list[str]
    ) -> int:
        dal = admin_db.dal
        role_id: int = dal.community_roles.insert(
            community_id=community_id,
            name=name,
            is_system=True,
            role_kind=kind,
            base_claims={"scopes": scopes},
        )
        dal.commit()
        return role_id

    async def test_update_member_role_promote_to_admin_requires_admin_scopes(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        # Caller (user_id=1) is not a member of this community at all --
        # cannot promote someone to an admin-kind role.
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(user_id=1),
            json={"roleId": self._rid(admin_db, admin_role)},
        )
        assert response.status_code == 403

    async def test_moderator_cannot_promote_to_admin_but_admin_can(
        self, client: Any, admin_db: Any
    ) -> None:
        """Scope-based escalation guard: the assigner must hold every scope of the assigned role."""
        community_id = _seed_community(admin_db)
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        mod_role = self._seed_role(
            admin_db, community_id, "moderator", 2, ["community:manage_channels"]
        )
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=7,
            role="moderator",
            community_role_id=mod_role,
        )
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=8,
            role="community-admin",
            community_role_id=admin_role,
        )
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        by_mod = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(user_id=7),
            json={"roleId": self._rid(admin_db, admin_role)},
        )
        assert by_mod.status_code == 403
        by_admin = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=_headers(user_id=8),
            json={"roleId": self._rid(admin_db, admin_role)},
        )
        assert by_admin.status_code == 200

    async def test_member_admin_actions_need_their_own_scopes(
        self, client: Any, admin_db: Any
    ) -> None:
        """A moderator-seeded member can remove members but cannot assign roles (own scope each)."""
        from services.scope_registry import MODERATOR_SEED_SCOPES

        community_id = _seed_community(admin_db)
        mod_role = self._seed_role(
            admin_db,
            community_id,
            "moderator",
            2,
            ["community:manage_channels", *MODERATOR_SEED_SCOPES],
        )
        vip = self._seed_role(admin_db, community_id, "vip", 0, ["community:read"])
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=7,
            role="moderator",
            community_role_id=mod_role,
        )
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        headers = _headers(user_id=7, scope="community:admin")
        denied = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=headers,
            json={"roleId": self._rid(admin_db, vip)},
        )
        assert denied.status_code == 403
        removed = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}",
            headers=headers,
            json={"reason": "spam"},
        )
        assert removed.status_code == 200

    async def test_custom_role_without_remove_scope_cannot_remove_or_adjust(
        self, client: Any, admin_db: Any
    ) -> None:
        community_id = _seed_community(admin_db)
        role = self._seed_role(admin_db, community_id, "custom", 0, ["community:manage_members"])
        _seed_member(
            admin_db, community_id=community_id, user_id=9, role="custom", community_role_id=role
        )
        _seed_member(admin_db, community_id=community_id, user_id=42, role="member")
        headers = _headers(user_id=9, scope="community:admin")
        removed = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}",
            headers=headers,
            json={},
        )
        assert removed.status_code == 403
        adjusted = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/reputation",
            headers=headers,
            json={"amount": 5},
        )
        assert adjusted.status_code == 403

    async def test_moderator_cannot_act_on_admin_member(self, client: Any, admin_db: Any) -> None:
        """Regression: moderator remove/reputation scopes must not reach an admin-kind target."""
        from services.scope_registry import MODERATOR_SEED_SCOPES

        community_id = _seed_community(admin_db)
        mod_role = self._seed_role(
            admin_db,
            community_id,
            "moderator",
            2,
            ["community:manage_channels", *MODERATOR_SEED_SCOPES],
        )
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=7,
            role="moderator",
            community_role_id=mod_role,
        )
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=42,
            role="community-admin",
            community_role_id=admin_role,
        )
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=8,
            role="community-admin",
            community_role_id=admin_role,
        )
        mod = _headers(user_id=7, scope="community:admin")
        removed = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}",
            headers=mod,
            json={"reason": "x"},
        )
        assert removed.status_code == 403
        adjusted = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/reputation",
            headers=mod,
            json={"amount": 5},
        )
        assert adjusted.status_code == 403
        demoted = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/role",
            headers=mod,
            json={"roleId": self._rid(admin_db, mod_role)},
        )
        assert demoted.status_code == 403
        # An admin-kind caller may still act on another admin.
        allowed = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}",
            headers=_headers(user_id=8, scope="community:admin"),
            json={"reason": "x"},
        )
        assert allowed.status_code == 200

    async def test_update_member_role_not_found_is_404(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(9999)}/role",
            headers=_headers(),
            json={"roleId": str(uuid.uuid4())},
        )
        assert response.status_code == 404

    async def test_adjust_reputation_relative(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42, reputation=600)
        _seed_admin_actor(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/reputation",
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
        _seed_admin_actor(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/reputation",
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
        _seed_admin_actor(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}/reputation",
            headers=_headers(),
            json={"amount": 0},
        )
        assert response.status_code == 400

    async def test_remove_member_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, member_count=2)
        _seed_member(admin_db, community_id=community_id, user_id=42)
        response = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}",
            headers=_headers(),
            json={"reason": "spam"},
        )
        assert response.status_code == 200

    async def test_remove_member_protects_last_admin(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        admin_role = self._seed_role(admin_db, community_id, "community-admin", 1, [])
        _seed_member(admin_db, community_id=community_id, user_id=42, community_role_id=admin_role)
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}"
        assert (await client.delete(url, headers=_headers(), json={})).status_code == 409
        _seed_member(admin_db, community_id=community_id, user_id=43, community_role_id=admin_role)
        assert (await client.delete(url, headers=_headers(), json={})).status_code == 200

    async def test_remove_member_without_scope_is_403(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42)
        response = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(42)}",
            headers=_headers(user_id=9, scope="community:read"),
            json={},
        )
        assert response.status_code == 403

    async def test_remove_member_cannot_remove_owner(self, client: Any, admin_db: Any) -> None:
        # Regression (PR #1539 review): the creator's appointee can never kick the owner.
        community_id = _seed_community(admin_db, member_count=2)
        _seed_owner_and_appointee(admin_db, community_id)
        response = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(OWNER)}",
            headers=_headers(user_id=APPOINTEE, scope=_MEMBER_SCOPE),
            json={},
        )
        assert response.status_code == 403
        dal = admin_db.dal
        owner = dal(dal.community_members.user_id == str(OWNER)).select().first()
        assert owner.is_active is True
        assert dal.communities[community_id].member_count == 2

    async def test_global_admin_may_remove_owner(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db, member_count=2)
        _seed_owner_and_appointee(admin_db, community_id)
        response = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(OWNER)}",
            headers=_headers(),  # `users:admin` = global admin bypass
            json={},
        )
        assert response.status_code == 200
        assert admin_db.dal.communities[community_id].member_count == 1

    async def test_remove_member_not_found_is_404(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/members/{uid(9999)}",
            headers=_headers(),
            json={},
        )
        assert response.status_code == 404


class TestTempPassword:
    #: A moderator holding the temp-password scope -- but not the admin-kind full set.
    MOD_SCOPES = ["members:temp_password.reset", "community:read"]

    @staticmethod
    def _url(admin_db: Any, community_id: int) -> str:
        return f"/api/v1/admin/{public_id(admin_db, community_id)}/temp-password"

    async def test_generate_temp_password_success(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_member(admin_db, community_id=community_id, user_id=42)
        response = await client.post(
            self._url(admin_db, community_id),
            headers=_headers(),
            json={"userIdentifier": "user42"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert "tempPassword" in body
        assert "-" in body["tempPassword"]
        # The row is bound to THAT member's hub user (login honors it).
        dal = admin_db.dal
        row = dal(dal.hub_temp_passwords.community_id == community_id).select().first()
        assert row.hub_user_id == 42

    async def test_non_member_identifier_is_404(self, client: Any, admin_db: Any) -> None:
        """Regression (PR #1542 review): an identifier that is not an active member HERE is 404."""
        community_id = _seed_community(admin_db)
        other = _seed_community(admin_db, name="other-community")
        _seed_member(admin_db, community_id=other, user_id=77)  # a member elsewhere only
        _seed_member(admin_db, community_id=community_id, user_id=78, is_active=False)
        for identifier in ("some-platform-user", "user77", "user78", "user77@example.com"):
            response = await client.post(
                self._url(admin_db, community_id),
                headers=_headers(),
                json={"userIdentifier": identifier},
            )
            assert response.status_code == 404, identifier
        dal = admin_db.dal
        assert dal(dal.hub_temp_passwords).count() == 0

    async def test_moderator_cannot_mint_for_owner_or_admin(
        self, client: Any, admin_db: Any
    ) -> None:
        """Regression (PR #1542 review): the shared owner + scope-subset target guard applies."""
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=43, role="member")
        mod_role = TestMembers._seed_role(admin_db, community_id, "moderator", 2, self.MOD_SCOPES)
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=9,
            role="moderator",
            community_role_id=mod_role,
        )
        mod = _headers(user_id=9, scope=_MEMBER_SCOPE)
        for target in (OWNER, APPOINTEE):
            response = await client.post(
                self._url(admin_db, community_id),
                headers=mod,
                json={"userIdentifier": f"user{target}"},
            )
            assert response.status_code == 403, target
        dal = admin_db.dal
        assert dal(dal.hub_temp_passwords).count() == 0
        # control: the same moderator may mint for a plain member it out-scopes
        ok = await client.post(
            self._url(admin_db, community_id), headers=mod, json={"userIdentifier": "user43"}
        )
        assert ok.status_code == 201
        assert dal(dal.hub_temp_passwords).select().first().hub_user_id == 43


class TestModules:
    async def test_get_modules_empty(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/modules", headers=_headers()
        )
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
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/modules", headers=_headers()
        )
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
            f"/api/v1/admin/{public_id(admin_db, community_id)}/modules/{mod_id}/config",
            headers=_headers(),
            json={"isEnabled": False},
        )
        assert response.status_code == 200

    async def test_update_module_config_not_found_is_404(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        response = await client.put(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/modules/9999/config",
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
            f"/api/v1/admin/{public_id(admin_db, community_id)}/modules/{mod_id}/config",
            headers=_headers(),
            json={},
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
            f"/api/v1/admin/{public_id(admin_db, community_id)}/modules/{hub_mod_id}/config",
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
            f"/api/v1/admin/{public_id(admin_db, community_id)}/connected-platforms",
            headers=_headers(),
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
        response = await client.get(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/commands", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert "success" not in body  # byte-faithful to Node: no success wrapper
        assert body["commands"][0]["command"] == "!help"
        assert body["commands"][0]["is_enabled"] is True


class TestAdminDedicatedScopes:
    """Each remaining admin route needs its own community scope, never the broad JWT scope."""

    ROUTES = (
        ("GET", "settings", "community.settings:manage"),
        ("GET", "members", "community.members:view"),
        ("GET", "modules", "community.modules:manage"),
        ("GET", "connected-platforms", "community.platforms:manage"),
        ("GET", "commands", "community.commands:manage"),
    )

    @staticmethod
    def _member_with(admin_db: Any, community_id: int, user_id: int, scopes: list[str]) -> None:
        dal = admin_db.dal
        role_id = dal.community_roles.insert(
            community_id=community_id,
            name=f"custom{user_id}",
            is_system=False,
            role_kind=0,
            base_claims={"scopes": scopes},
        )
        dal.commit()
        _seed_member(
            admin_db,
            community_id=community_id,
            user_id=user_id,
            role="custom",
            community_role_id=role_id,
        )

    @pytest.mark.parametrize(("method", "path", "scope"), ROUTES)
    async def test_broad_scopes_and_jwt_admin_scope_are_refused(
        self, client: Any, admin_db: Any, method: str, path: str, scope: str
    ) -> None:
        community_id = _seed_community(admin_db)
        self._member_with(
            admin_db, community_id, 9, ["community:manage_members", "community:manage_channels"]
        )
        response = await client.open(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/{path}",
            method=method,
            headers=_headers(user_id=9, scope="community:admin"),
        )
        assert response.status_code == 403

    @pytest.mark.parametrize(("method", "path", "scope"), ROUTES)
    async def test_exact_scope_admits_and_only_that_route(
        self, client: Any, admin_db: Any, method: str, path: str, scope: str
    ) -> None:
        community_id = _seed_community(admin_db)
        self._member_with(admin_db, community_id, 10, [scope])
        headers = _headers(user_id=10, scope="")
        allowed = await client.open(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/{path}",
            method=method,
            headers=headers,
        )
        assert allowed.status_code != 403
        for other_method, other_path, other_scope in self.ROUTES:
            if other_scope == scope:
                continue
            denied = await client.open(
                f"/api/v1/admin/{public_id(admin_db, community_id)}/{other_path}",
                method=other_method,
                headers=headers,
            )
            assert denied.status_code == 403, other_path

    async def test_temp_password_needs_its_own_scope(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        self._member_with(admin_db, community_id, 11, ["community.members:view", "members:remove"])
        response = await client.post(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/temp-password",
            headers=_headers(user_id=11, scope=""),
            json={"userId": 42},
        )
        assert response.status_code == 403


class TestMemberTargetGuardParity:
    """Every admin route that acts on another member goes through the one owner/scope rule."""

    #: (rule suffix after `/<community>/`, method) for each member-targeting route.
    ROUTES = {
        ("members/<user_id>/role", "PUT"),
        ("members/<user_id>/reputation", "PUT"),
        ("members/<user_id>", "DELETE"),
        ("reputation/soft-bans/<user_id>", "PUT"),
        ("reputation/soft-bans/<user_id>", "DELETE"),
    }

    @staticmethod
    def _member_target_routes(app: Quart) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for rule in app.url_map.iter_rules():
            if "user_id" not in rule.arguments or not rule.rule.startswith("/api/v1/admin/"):
                continue
            suffix = rule.rule.split("/", 5)[5].replace("<uuuid:user_id>", "<user_id>")
            if not suffix.startswith(("members/", "reputation/")):
                continue
            for method in rule.methods - {"HEAD", "OPTIONS"}:
                found.add((suffix, method))
        return found

    async def test_routes_enumerated_and_each_invokes_the_shared_rule(
        self, app: Quart, client: Any, admin_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services import community_authz

        assert self._member_target_routes(app) == self.ROUTES  # a new route must be added here
        community_id = _seed_community(admin_db, member_count=3)
        _seed_owner_and_appointee(admin_db, community_id)
        _seed_linked(admin_db, community_id=community_id, user_id=49, role="member")
        bodies = {
            "members/<user_id>/role": _role_json(admin_db, community_id, "member"),
            "members/<user_id>/reputation": {"amount": 1},
            "members/<user_id>": {},
            "reputation/soft-bans/<user_id>": {},
        }
        calls: list[int] = []
        original = community_authz.check_target_member

        def spy(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            original(*args, **kwargs)

        monkeypatch.setattr(community_authz, "check_target_member", spy)
        examined = 0
        for index, (suffix, method) in enumerate(sorted(self.ROUTES)):
            before = len(calls)
            target = 50 + index  # a fresh member per route (the remove route deactivates it)
            _seed_linked(admin_db, community_id=community_id, user_id=target, role="member")
            url = f"/api/v1/admin/{public_id(admin_db, community_id)}/" + suffix.replace(
                "<user_id>", uid(target)
            )
            response = await client.open(
                url, method=method, headers=_headers(), json=bodies[suffix]
            )
            assert len(calls) > before, f"{method} {suffix} skipped the shared guard"
            assert response.status_code != 500
            examined += 1
        assert examined == len(self.ROUTES) == 5

    @pytest.mark.parametrize(
        "suffix", ["reputation/soft-bans/<user_id>", "members/<user_id>/reputation"]
    )
    async def test_appointee_cannot_act_on_owner(
        self, client: Any, admin_db: Any, suffix: str
    ) -> None:
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        url = f"/api/v1/admin/{public_id(admin_db, community_id)}/" + suffix.replace(
            "<user_id>", uid(OWNER)
        )
        body: dict[str, Any] = {"amount": 1} if suffix.endswith("reputation") else {}
        response = await client.open(
            url,
            method="PUT",
            headers=_headers(user_id=APPOINTEE, scope=_MEMBER_SCOPE),
            json=body,
        )
        assert response.status_code == 403
        # control: a global admin (bypass) is not blocked by the owner rule
        _seed_admin_actor(admin_db)
        ok = await client.open(url, method="PUT", headers=_headers(), json=body)
        assert ok.status_code != 403, await ok.get_json()

    async def test_appointee_cannot_clear_owner_soft_ban(self, client: Any, admin_db: Any) -> None:
        community_id = _seed_community(admin_db)
        _seed_owner_and_appointee(admin_db, community_id)
        response = await client.delete(
            f"/api/v1/admin/{public_id(admin_db, community_id)}/reputation/soft-bans/{uid(OWNER)}",
            headers=_headers(user_id=APPOINTEE, scope=_MEMBER_SCOPE),
        )
        assert response.status_code == 403
