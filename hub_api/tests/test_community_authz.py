"""`services/community_authz.py` -- direct + integration coverage.

`tests/test_v1_workflow_blueprint.py`/`test_v1_github_sync_blueprint.py`
exercise the "not an admin -> 403" and "is a community member with the
right scope -> 200" paths through the real HTTP blueprints already --
`TestJwtRolesDirect`/`TestRequireCommunityAdminBypasses` below close the
remaining branches for the M-automation group's `require_community_admin()`:
the three bypasses (`super-admin`, `tenant-admin`, `platform-admin`), the
"member but wrong scope" 403, and `_jwt_roles()`'s own defensive branches
(unreachable through the normal HTTP flow, since `tenant_middleware`/
`get_current_user_id` already validate the bearer token before
`_jwt_roles()` ever runs -- covered here as a direct unit test of the
function instead).

`TestParseClaimsScopes`/`TestDecodeCaller`/`TestAuthorizeCommunityMissingTenantContext`
below cover the M7 Streaming group's own `_scoped` variant instead --
`_decode_caller`/`authorize_community`'s early-return branches (missing
bearer token, invalid token, missing `sub` claim, unresolved tenant
context) are similarly unreachable through the normal route path once
`tenant_middleware` has already run (it 401s on exactly those same
conditions first) -- same "defensive, second independent decode" shape
`services.current_user`'s own docstring describes. Tested directly here
against a minimal request-like stand-in, mirroring how
`tests/test_event_calendar_proxy.py` unit-tests `event_calendar_proxy.py`
below the blueprint layer.

Security hotfix (cross-tenant IDOR, tenant-admin bypass): both
`require_community_admin`'s tenant-admin bypass AND
`resolve_community_membership_scoped`'s own tenant-admin bypass used to
grant access as soon as the caller administered *some* tenant, without
confirming the *target* `community_id` actually belonged to it.
`TestRequireCommunityAdminBypasses.
test_tenant_admin_cannot_bypass_community_in_different_tenant` and
`TestResolveCommunityMembershipScopedTenantAdminBypass` below cover the
fix (`community_belongs_to_tenant()`) for both call paths, plus a
positive case each confirming a tenant-admin still authorizes a community
IN their own tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import jwt as pyjwt
import pytest
from flask_core.auth import create_jwt_token
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.workflow as workflow_module
from blueprints.v1.workflow import workflow_bp
from services.community_authz import (
    _decode_caller,
    _jwt_roles,
    _parse_claims_scopes,
    authorize_community,
    community_belongs_to_tenant,
    is_super_admin,
    require_community_admin_scoped,
    resolve_community_membership_scoped,
)
from services.errors import ApiError
from services.workflow_service import ProxyResult
from tests.conftest import SECRET_KEY, TENANT_SLUG, insert_global_admin

COMMUNITY_ID = 1


@dataclass(slots=True)
class _FakeRequest:
    """Minimal stand-in for `quart.Request` -- only the attributes these functions touch."""

    headers: dict[str, str] = field(default_factory=dict)
    tenant_context: Any = None


class TestParseClaimsScopes:
    def test_none_is_empty(self) -> None:
        assert _parse_claims_scopes(None) == frozenset()

    def test_valid_json_string_list(self) -> None:
        assert _parse_claims_scopes('["a:read", "b:write"]') == {"a:read", "b:write"}

    def test_invalid_json_string_is_empty(self) -> None:
        assert _parse_claims_scopes("not json") == frozenset()

    def test_list(self) -> None:
        assert _parse_claims_scopes(["a:read"]) == {"a:read"}

    def test_dict_with_scopes(self) -> None:
        assert _parse_claims_scopes({"scopes": ["a:read"]}) == {"a:read"}

    def test_dict_without_scopes_key_is_empty(self) -> None:
        assert _parse_claims_scopes({"other": True}) == frozenset()

    def test_unrecognized_type_is_empty(self) -> None:
        assert _parse_claims_scopes(42) == frozenset()


class TestDecodeCaller:
    def test_missing_authorization_header_is_401(self) -> None:
        with pytest.raises(ApiError) as exc_info:
            _decode_caller(_FakeRequest(headers={}))
        assert exc_info.value.status_code == 401

    def test_non_bearer_header_is_401(self) -> None:
        with pytest.raises(ApiError) as exc_info:
            _decode_caller(_FakeRequest(headers={"Authorization": "Basic xyz"}))
        assert exc_info.value.status_code == 401

    def test_invalid_token_is_401(self) -> None:
        with pytest.raises(ApiError) as exc_info:
            _decode_caller(_FakeRequest(headers={"Authorization": "Bearer not-a-real-jwt"}))
        assert exc_info.value.status_code == 401

    def test_token_missing_subject_claim_is_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.community_authz as module

        monkeypatch.setattr(module, "verify_jwt_token", lambda token, key: {"roles": []})
        with pytest.raises(ApiError) as exc_info:
            _decode_caller(_FakeRequest(headers={"Authorization": "Bearer x"}))
        assert exc_info.value.status_code == 401

    def test_success_returns_user_id_and_roles(self) -> None:
        token = create_jwt_token(
            user_id="7",
            username="alice",
            email="alice@example.com",
            roles=["super_admin"],
            secret_key=SECRET_KEY,
            tenant="acme-corp",
        )
        user_id, roles = _decode_caller(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
        assert user_id == 7
        assert roles == ["super_admin"]


class TestAuthorizeCommunityMissingTenantContext:
    async def test_no_tenant_context_is_403(self) -> None:
        with pytest.raises(ApiError) as exc_info:
            await authorize_community(
                _FakeRequest(headers={}, tenant_context=None),
                async_dal=None,
                dal=None,
                community_id=1,
                admin=True,
            )
        assert exc_info.value.status_code == 403


class _StubWorkflowCore:
    """Just enough of `WorkflowCoreProxyClient` for `list_workflows` to succeed without real I/O."""

    async def request(
        self, method: str, path: str, *, json_body: object = None, params: object = None
    ) -> ProxyResult:
        return ProxyResult(ok=True, status_code=200, body={"workflows": [], "pagination": {}})


@pytest.fixture(autouse=True)
def _stub_workflow_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_module, "_client", _StubWorkflowCore())


class TestJwtRolesDirect:
    """Direct unit tests -- these branches are unreachable through the normal HTTP flow."""

    def test_no_authorization_header_is_empty(self) -> None:
        request = Mock()
        request.headers = {}
        assert _jwt_roles(request) == frozenset()

    def test_non_bearer_header_is_empty(self) -> None:
        request = Mock()
        request.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        assert _jwt_roles(request) == frozenset()

    def test_invalid_token_is_empty(self) -> None:
        request = Mock()
        request.headers = {"Authorization": "Bearer not-a-real-jwt"}
        assert _jwt_roles(request) == frozenset()

    def test_non_list_roles_claim_is_empty(self) -> None:
        """`create_jwt_token()` only accepts `List[str]` -- hand-craft the payload instead."""
        token = pyjwt.encode(
            {
                "sub": "1",
                "tenant": TENANT_SLUG,
                "roles": "platform-admin",  # a bare string, not a list
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        request = Mock()
        request.headers = {"Authorization": f"Bearer {token}"}
        assert _jwt_roles(request) == frozenset()

    def test_platform_admin_role_is_present(self) -> None:
        token = create_jwt_token(
            user_id="1",
            username="u",
            email="u@example.com",
            roles=["platform-admin"],
            secret_key=SECRET_KEY,
            tenant=TENANT_SLUG,
        )
        request = Mock()
        request.headers = {"Authorization": f"Bearer {token}"}
        assert "platform-admin" in _jwt_roles(request)


class TestRequireCommunityAdminBypasses:
    """Integration coverage via `workflow.py`'s `list_workflows` -- the thinnest gated route."""

    def _app(self, automation_db: Any) -> Quart:
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(workflow_bp)
        quart_app.config["dal"] = automation_db.dal
        quart_app.config["async_dal"] = automation_db
        return quart_app

    def _token(self, *, user_id: str, roles: list[str] | None = None) -> str:
        return create_jwt_token(
            user_id=user_id,
            username="u",
            email="u@example.com",
            roles=roles or [],
            secret_key=SECRET_KEY,
            tenant=TENANT_SLUG,
        )

    async def test_super_admin_bypasses_community_membership(self, automation_db: Any) -> None:
        dal = automation_db.dal
        user_id = insert_global_admin(dal, username="root")
        dal.commit()
        app = self._app(automation_db)
        client = app.test_client()
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers={"Authorization": f"Bearer {self._token(user_id=str(user_id))}"},
        )
        assert response.status_code == 200

    async def test_tenant_admin_bypasses_community_membership(self, automation_db: Any) -> None:
        """Positive case: tenant-admin authorizing a community IN their own tenant succeeds."""
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="tadmin")
        tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        dal.tenant_admins.insert(tenant_id=tenant_row.id, user_id=user_id, role="tenant-admin")
        community_id = dal.communities.insert(name="own-tenant-community", tenant_id=tenant_row.id)
        dal.commit()
        app = self._app(automation_db)
        client = app.test_client()
        response = await client.get(
            f"/api/v1/admin/{community_id}/workflows",
            headers={"Authorization": f"Bearer {self._token(user_id=str(user_id))}"},
        )
        assert response.status_code == 200

    async def test_tenant_admin_cannot_bypass_community_in_different_tenant(
        self, automation_db: Any
    ) -> None:
        # regression: cross-tenant IDOR (automated security review)
        #
        # A tenant-admin of "acme-corp" must NOT be able to use that
        # bypass to reach a community owned by a different tenant --
        # `require_community_admin`'s tenant-admin bypass previously
        # returned as soon as a `tenant_admins` row for the CALLER's own
        # tenant existed, without ever checking that the *target*
        # `community_id` in the URL also belonged to that tenant.
        #
        # Fail-first proof (executed, not narrated): temporarily reverted
        # `require_community_admin`'s tenant-admin bypass to `if ta_rows:
        # return` (dropping the `community_belongs_to_tenant()` cross-check)
        # -- this test went red (200 instead of the expected 403), confirming
        # it actually exercises the vulnerable branch. Reverted, green again.
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="tadmin")
        own_tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        dal.tenant_admins.insert(tenant_id=own_tenant_row.id, user_id=user_id, role="tenant-admin")
        other_tenant_id = dal.tenants.insert(slug="other-tenant", is_active=True)
        other_community_id = dal.communities.insert(
            name="other-tenant-community", tenant_id=other_tenant_id
        )
        dal.commit()
        app = self._app(automation_db)
        client = app.test_client()
        response = await client.get(
            f"/api/v1/admin/{other_community_id}/workflows",
            headers={"Authorization": f"Bearer {self._token(user_id=str(user_id))}"},
        )
        assert response.status_code == 403

    async def test_platform_admin_role_bypasses_community_membership(
        self, automation_db: Any
    ) -> None:
        app = self._app(automation_db)
        client = app.test_client()
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers={
                "Authorization": f"Bearer {self._token(user_id='999', roles=['platform-admin'])}"
            },
        )
        assert response.status_code == 200

    async def test_member_with_wrong_scope_is_403(self, automation_db: Any) -> None:
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="member")
        role_id = dal.community_roles.insert(
            community_id=COMMUNITY_ID, name="viewer", base_claims={"scopes": ["community:read"]}
        )
        dal.community_members.insert(
            community_id=COMMUNITY_ID,
            user_id=str(user_id),
            role="viewer",
            is_active=True,
            community_role_id=role_id,
        )
        dal.commit()
        app = self._app(automation_db)
        client = app.test_client()
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers={"Authorization": f"Bearer {self._token(user_id=str(user_id))}"},
        )
        assert response.status_code == 403

    async def test_member_with_no_role_assigned_is_403(self, automation_db: Any) -> None:
        """Legacy member row with `community_role_id=None` -- LEFT JOIN finds nothing."""
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="legacy-member")
        dal.community_members.insert(
            community_id=COMMUNITY_ID, user_id=str(user_id), role="member", is_active=True
        )
        dal.commit()
        app = self._app(automation_db)
        client = app.test_client()
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers={"Authorization": f"Bearer {self._token(user_id=str(user_id))}"},
        )
        assert response.status_code == 403


class TestResolveCommunityMembershipScopedTenantAdminBypass:
    """`resolve_community_membership_scoped`'s tenant-admin bypass had the same IDOR shape.

    Same security hotfix, same file: the `_is_tenant_admin()` bypass check ran BEFORE the
    `communities.tenant_id` ownership check, so it never actually benefited from that check
    despite this module's own docstring claiming otherwise. Covered here directly (not via
    a blueprint) against `automation_db`, which already binds `communities`/`tenant_admins`/
    `community_members`/`community_roles`/`hub_users` -- no need for the M7 Streaming group's
    `streaming_db` fixture just to exercise this function in isolation.
    """

    async def test_tenant_admin_bypass_within_own_tenant_is_admin(self, automation_db: Any) -> None:
        """Positive case: tenant-admin authorizing a community IN their own tenant succeeds."""
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="tadmin")
        tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        dal.tenant_admins.insert(tenant_id=tenant_row.id, user_id=user_id, role="tenant-admin")
        community_id = dal.communities.insert(name="own-tenant-community", tenant_id=tenant_row.id)
        dal.commit()

        membership = await resolve_community_membership_scoped(
            automation_db,
            dal,
            community_id=community_id,
            user_id=user_id,
            tenant_id=tenant_row.id,
            roles_claim=[],
        )
        assert membership is not None
        assert membership.bypass is True
        assert membership.is_admin is True

        admin_membership = await require_community_admin_scoped(
            automation_db,
            dal,
            community_id=community_id,
            user_id=user_id,
            tenant_id=tenant_row.id,
            roles_claim=[],
        )
        assert admin_membership.is_admin is True

    async def test_tenant_admin_bypass_cannot_reach_community_in_different_tenant(
        self, automation_db: Any
    ) -> None:
        # regression: cross-tenant IDOR (automated security review)
        #
        # Fail-first proof (executed, not narrated): temporarily reverted
        # `resolve_community_membership_scoped` to check `_is_tenant_admin()`
        # BEFORE the `community_belongs_to_tenant()` ownership check (the
        # pre-fix ordering) -- this test went red (`membership is not None`
        # / `is_admin=True` instead of the expected `None` / 403).
        # Reverted, green again.
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="tadmin")
        own_tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        dal.tenant_admins.insert(tenant_id=own_tenant_row.id, user_id=user_id, role="tenant-admin")
        other_tenant_id = dal.tenants.insert(slug="other-tenant", is_active=True)
        other_community_id = dal.communities.insert(
            name="other-tenant-community", tenant_id=other_tenant_id
        )
        dal.commit()

        membership = await resolve_community_membership_scoped(
            automation_db,
            dal,
            community_id=other_community_id,
            user_id=user_id,
            tenant_id=own_tenant_row.id,
            roles_claim=[],
        )
        assert membership is None

        with pytest.raises(ApiError) as exc_info:
            await require_community_admin_scoped(
                automation_db,
                dal,
                community_id=other_community_id,
                user_id=user_id,
                tenant_id=own_tenant_row.id,
                roles_claim=[],
            )
        assert exc_info.value.status_code == 403


class TestIsSuperAdmin:
    """Direct unit coverage for `is_super_admin()` -- the consolidation target.

    Promoted from `_is_super_admin` (module-private) to this public,
    DB-backed shared primitive during the security-helper consolidation --
    `services/community_access.py`'s `require_community_admin`/
    `require_community_member` now call this instead of their own former
    JWT-`roles`-claim reimplementation (removed; see `community_access.py`'s
    own docstring). `test_super_admin_bypasses_community_membership` above
    already covers the True path end-to-end through `require_community_admin`
    -- these three close the direct-unit branches (True, DB-false, missing
    row) that integration alone doesn't isolate.
    """

    async def test_true_for_flagged_user(self, automation_db: Any) -> None:
        dal = automation_db.dal
        user_id = insert_global_admin(dal, username="root")
        dal.commit()
        assert await is_super_admin(automation_db, dal, user_id=user_id) is True

    async def test_false_for_unflagged_user(self, automation_db: Any) -> None:
        dal = automation_db.dal
        user_id = dal.hub_users.insert(username="regular")
        dal.commit()
        assert await is_super_admin(automation_db, dal, user_id=user_id) is False

    async def test_false_for_missing_user_row(self, automation_db: Any) -> None:
        """Fail closed: a `user_id` with no `hub_users` row at all is never a super admin."""
        dal = automation_db.dal
        assert await is_super_admin(automation_db, dal, user_id=999999) is False


class TestCommunityBelongsToTenant:
    """Direct unit coverage for `community_belongs_to_tenant()` -- the #226 IDOR-fix primitive.

    Promoted from `_community_belongs_to_tenant` (module-private) to this
    public, shared primitive -- `services/community_access.py`'s own tenant
    pre-check (`_require_community_in_tenant`) now calls this instead of its
    former separate `tenant_scoped()`-based reimplementation of the
    identical check. `TestRequireCommunityAdminBypasses.
    test_tenant_admin_cannot_bypass_community_in_different_tenant` above
    already covers this through the full `require_community_admin` bypass
    flow -- these close the direct-unit branches.
    """

    async def test_true_for_matching_tenant(self, automation_db: Any) -> None:
        dal = automation_db.dal
        tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        community_id = dal.communities.insert(name="c1", tenant_id=tenant_row.id)
        dal.commit()
        assert (
            await community_belongs_to_tenant(
                automation_db, dal, community_id=community_id, tenant_id=tenant_row.id
            )
            is True
        )

    async def test_false_for_different_tenant(self, automation_db: Any) -> None:
        dal = automation_db.dal
        tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        other_tenant_id = dal.tenants.insert(slug="different-tenant", is_active=True)
        community_id = dal.communities.insert(name="c1", tenant_id=tenant_row.id)
        dal.commit()
        assert (
            await community_belongs_to_tenant(
                automation_db, dal, community_id=community_id, tenant_id=other_tenant_id
            )
            is False
        )

    async def test_false_for_nonexistent_community(self, automation_db: Any) -> None:
        dal = automation_db.dal
        tenant_row = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        assert (
            await community_belongs_to_tenant(
                automation_db, dal, community_id=999999, tenant_id=tenant_row.id
            )
            is False
        )
