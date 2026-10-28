"""`blueprints/v1/user_management.py` -- the M1 superadmin user-management group.

Standalone Quart app registering only `user_management_bp`, matching
`test_platform_blueprint.py`'s pattern. `require_scope("users:admin")` is
this group's fail-first scope-check proof (see this file's module-level
note below).

Fail-first proof (executed, not narrated): temporarily swapped
`require_scope("users:admin")` for `require_scope("users:read")` on
`list_users`' decorator chain -- `test_list_users_wrong_scope_is_403`
went red (200 instead of 403, since the admin-bundle token in
`auth_headers` no longer matched); reverted, green again.

`TestPrivilegeEscalationRegression` (C3, A01/BOLA fix): fail-first proof
executed against the pre-fix code (temporarily reverting both
`flask_core.auth.SCOPE_BUNDLES["tenant"]["admin"]`'s removal of
`users:admin` AND `assign_super_admin_role`'s DB-authoritative
`caller_id` gate) -- `test_tenant_owner_cannot_self_promote_to_platform_
super_admin` and `test_tenant_owner_cannot_modify_cross_tenant_user`
both went green (200, not 403) against the vulnerable code, i.e. red
against the fix's own expectations; reverted, red->green restored. See
`services/user_management_service.py`'s module docstring and
`flask_core/auth.py`'s `SCOPE_BUNDLES["tenant"]` comment for the full
root-cause writeup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.auth import SCOPE_BUNDLES
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.user_management import user_management_bp
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_user_token, seed_super_admin


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(user_management_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(auth_db: Any, *, email: str = "target@example.com") -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email,
        username=email,
        is_active=True,
        email_verified=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


def _admin_headers(*, user_id: int = 1) -> dict[str, str]:
    # global:admin bundle -- SCOPE_BUNDLES["global"]["admin"] includes
    # "users:admin", the exact scope this blueprint requires.
    token = make_user_token(
        user_id=user_id,
        scope="*:read *:write *:admin *:delete settings:write users:admin",
        tenant=TENANT_SLUG,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_tenant_owner(auth_db: Any, *, user_id: int, tenant_slug: str = TENANT_SLUG) -> None:
    """Insert a `hub_users` row (lands on `user_id`) plus a `tenant_admins` row.

    `role="tenant-owner"` -- `auth_service.create_session_token` reads this
    to grant `SCOPE_BUNDLES["tenant"]["admin"]` at login. The `hub_users`
    row itself is NOT `is_super_admin`.
    """
    dal = auth_db.dal
    inserted_id: int = dal.hub_users.insert(
        email=f"owner-{user_id}@example.com",
        username=f"owner-{user_id}",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert inserted_id == user_id, (
        f"hub_users autoincrement landed on {inserted_id}, expected {user_id} -- "
        "insert this before any other hub_users row in the same test"
    )
    tenant = dal(dal.tenants.slug == tenant_slug).select().first()
    dal.tenant_admins.insert(
        tenant_id=tenant.id, user_id=user_id, role="tenant-owner", created_at=datetime.now(UTC)
    )
    dal.commit()


def _tenant_owner_headers(*, user_id: int, tenant_slug: str = TENANT_SLUG) -> dict[str, str]:
    """A token carrying EXACTLY the scopes `create_session_token` mints for a real tenant owner.

    `tenant_admins.role == "tenant-owner"` -- `SCOPE_BUNDLES["global"]
    ["viewer"]` (every session) union `SCOPE_BUNDLES["tenant"]["admin"]` (see
    `services/auth_service.py::create_session_token`). Built from the real
    bundle dict, not a hardcoded string, so this test fails loudly if
    `users:admin` is ever re-added to the tenant bundle (the C3 regression).
    """
    scopes = set(SCOPE_BUNDLES["global"]["viewer"]) | set(SCOPE_BUNDLES["tenant"]["admin"])
    token = make_user_token(user_id=user_id, scope=" ".join(sorted(scopes)), tenant=tenant_slug)
    return {"Authorization": f"Bearer {token}"}


class TestScopeEnforcement:
    async def test_list_users_no_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/users")
        assert response.status_code == 401

    async def test_list_users_wrong_scope_is_403(self, client: Any, auth_headers: Any) -> None:
        """The representative scope-check: a valid token WITHOUT users:admin is refused."""
        response = await client.get(
            "/api/v1/superadmin/users", headers=auth_headers(scope="platform:read")
        )
        assert response.status_code == 403

    async def test_list_users_with_scope_returns_200(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/users", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["pagination"]["page"] == 1


class TestCreateUser:
    async def test_create_user_success(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/superadmin/users",
            headers=_admin_headers(),
            json={"email": "newadmin@example.com", "password": "hunter22"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["user"]["email"] == "newadmin@example.com"

    async def test_create_user_duplicate_email_is_409(self, client: Any, auth_db: Any) -> None:
        _seed_user(auth_db, email="dupe@example.com")
        response = await client.post(
            "/api/v1/superadmin/users",
            headers=_admin_headers(),
            json={"email": "dupe@example.com", "password": "hunter22"},
        )
        assert response.status_code == 409


class TestGetUpdateDeleteUser:
    async def test_get_user_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/users/9999", headers=_admin_headers())
        assert response.status_code == 404

    async def test_get_user_found(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        response = await client.get(f"/api/v1/superadmin/users/{user_id}", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["user"]["id"] == user_id

    async def test_update_user_success(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        response = await client.put(
            f"/api/v1/superadmin/users/{user_id}",
            headers=_admin_headers(),
            json={"isActive": False},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["user"]["isActive"] is False

    async def test_delete_user_cannot_delete_self(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db, email="self@example.com")
        response = await client.delete(
            f"/api/v1/superadmin/users/{user_id}", headers=_admin_headers(user_id=user_id)
        )
        assert response.status_code == 403


class TestRoleAssignment:
    async def test_assign_super_admin_role(self, client: Any, auth_db: Any) -> None:
        # assign_super_admin_role additionally requires the caller to be a
        # DB-authoritative platform super admin (services.community_authz.
        # is_super_admin) -- see this module's C3 fix note. Seed the caller
        # (lands on id 1, matching _admin_headers()'s default) BEFORE the
        # target user so hub_users' autoincrement doesn't collide.
        seed_super_admin(auth_db, user_id=1, bootstrap=True)
        user_id = _seed_user(auth_db)
        response = await client.post(
            f"/api/v1/superadmin/users/{user_id}/super-admin-role",
            headers=_admin_headers(),
            json={"grant": True},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert "granted" in body["message"]

    async def test_assign_super_admin_role_non_super_admin_caller_is_403(
        self, client: Any, auth_db: Any
    ) -> None:
        """Caller holds the `users:admin` scope but is NOT a DB super admin.

        E.g. a stale/forged token, or a scope granted by some future bundle
        change. The DB-authoritative gate must still refuse (belt-and-
        suspenders alongside the `SCOPE_BUNDLES` fix -- see
        `TestPrivilegeEscalationRegression` for the actual tenant-owner
        scenario this reproduces end-to-end).
        """
        user_id = _seed_user(auth_db)
        response = await client.post(
            f"/api/v1/superadmin/users/{user_id}/super-admin-role",
            headers=_admin_headers(user_id=999),  # no hub_users row at all
            json={"grant": True},
        )
        assert response.status_code == 403

    async def test_assign_analytics_consumer_role(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        response = await client.post(
            f"/api/v1/superadmin/users/{user_id}/analytics-consumer-role",
            headers=_admin_headers(),
            json={"enabled": True},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["user"]["isAnalyticsConsumer"] is True


class TestPrivilegeEscalationRegression:
    """C3 (CRITICAL, A01/BOLA): a tenant owner must never satisfy this blueprint's gate.

    A tenant owner's real, `create_session_token`-minted scopes must never
    satisfy this blueprint's `require_scope("users:admin")` gate. Every
    token below is built from the actual `SCOPE_BUNDLES` dict
    (`_tenant_owner_headers`), not a hand-picked string, so these tests fail
    loudly if the bundle collision (`flask_core.auth.SCOPE_BUNDLES["tenant"]
    ["admin"]` re-granting `users:admin`) ever regresses -- independent of
    the additional DB-authoritative gate on `assign_super_admin_role`
    (`TestRoleAssignment` above).
    """

    async def test_tenant_owner_cannot_self_promote_to_platform_super_admin(
        self, client: Any, auth_db: Any
    ) -> None:
        """The exact reported exploit: a tenant owner self-promotes to platform super admin.

        A 403 alone is conclusive here -- `assign_super_admin_role` raises
        `forbidden()` before ever calling `update_async`, so there is no
        mutation to additionally verify (and no need to re-read via
        `auth_db.dal` cross-thread from the `update_async` executor -- a
        separate, pre-existing AsyncDAL visibility quirk unrelated to this
        fix).
        """
        _seed_tenant_owner(auth_db, user_id=1)
        response = await client.post(
            "/api/v1/superadmin/users/1/super-admin-role",
            headers=_tenant_owner_headers(user_id=1),
            json={"grant": True},
        )
        assert response.status_code == 403

    async def test_tenant_owner_cannot_promote_another_user_to_platform_super_admin(
        self, client: Any, auth_db: Any
    ) -> None:
        """Same exploit, granting to a different (non-self) target."""
        _seed_tenant_owner(auth_db, user_id=1)
        target_id = _seed_user(auth_db, email="victim@example.com")
        response = await client.post(
            f"/api/v1/superadmin/users/{target_id}/super-admin-role",
            headers=_tenant_owner_headers(user_id=1),
            json={"grant": True},
        )
        assert response.status_code == 403

    async def test_tenant_owner_cannot_modify_cross_tenant_user(
        self, client: Any, auth_db: Any
    ) -> None:
        """A tenant-A owner must not reach ANY `/api/v1/superadmin/users/*` action.

        Including plain `update_user` -- against a user who belongs to a
        different tenant (tenant B).
        """
        dal = auth_db.dal
        dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True)
        dal.commit()
        _seed_tenant_owner(auth_db, user_id=1, tenant_slug=TENANT_SLUG)
        tenant_b_user_id = _seed_user(auth_db, email="tenant-b-user@example.com")
        other_tenant = dal(dal.tenants.slug == OTHER_TENANT_SLUG).select().first()
        dal.tenant_admins.insert(
            tenant_id=other_tenant.id,
            user_id=tenant_b_user_id,
            role="tenant-owner",
            created_at=datetime.now(UTC),
        )
        dal.commit()

        response = await client.put(
            f"/api/v1/superadmin/users/{tenant_b_user_id}",
            headers=_tenant_owner_headers(user_id=1, tenant_slug=TENANT_SLUG),
            json={"isActive": False},
        )
        assert response.status_code in (403, 404)

    async def test_tenant_owner_list_users_is_403(self, client: Any, auth_db: Any) -> None:
        """Sanity check the fix isn't `assign_super_admin_role`-only.

        The WHOLE blueprint (list/get/create/update/delete/role-assignment)
        was reachable via the same collided scope; `list_users` is the
        representative read-path check.
        """
        _seed_tenant_owner(auth_db, user_id=1)
        response = await client.get(
            "/api/v1/superadmin/users", headers=_tenant_owner_headers(user_id=1)
        )
        assert response.status_code == 403

    async def test_platform_super_admin_role_management_still_works(
        self, client: Any, auth_db: Any
    ) -> None:
        """Non-regression: a genuine platform super admin's role management still works.

        Confirms this blueprint is unaffected by the fix for the caller it
        was always meant to serve.
        """
        seed_super_admin(auth_db, user_id=1, bootstrap=True)
        target_id = _seed_user(auth_db)
        response = await client.post(
            f"/api/v1/superadmin/users/{target_id}/super-admin-role",
            headers=_admin_headers(user_id=1),
            json={"grant": True},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert "granted" in body["message"]


class TestListLimitIsBounded:
    """gh security-review HIGH: `?limit=` was unbounded at this route.

    `list_users` had its own defense-in-depth already (`user_management_
    service.py::list_users` re-clamps `limit = min(100, max(1, limit))`
    before it ever reaches a query, and `PaginationDTO`'s own construction
    independently re-clamps the *displayed* value too) -- routing through
    `services/pagination.py::parse_limit()` here closes the route-level
    gap for consistency with every other list endpoint, not because this
    one specific endpoint was independently exploitable end-to-end. The
    genuinely-exploitable case (no clamp anywhere else in the call chain)
    is `services/community_inventory.py::get_audit_log`'s raw
    parameterized SQL -- see `tests/test_community_inventory.py::
    TestListCheckoutsAuditAndSummary::test_audit_log_limit_query_param_is_
    capped_before_reaching_raw_sql` for that endpoint's real fail-first
    proof, and `tests/test_pagination.py` for `parse_limit()`'s own
    unit-level fail-first proof.
    """

    async def test_absurd_limit_query_param_is_capped_not_passed_through(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/superadmin/users?limit=999999", headers=_admin_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["limit"] == 100

    async def test_negative_limit_query_param_is_clamped_to_minimum(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/users?limit=-5", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["limit"] == 1

    async def test_reasonable_limit_query_param_passes_through_unchanged(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/users?limit=10", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["limit"] == 10
