"""`blueprints/v1/join_request.py` -- the M2 Core Tenancy-Misc `joinRequest` group.

Standalone Quart app registering only `join_request_bp` (mirrors
`test_v1_auth_blueprint.py`'s own pattern) against the `auth_db` fixture
(`tests/conftest.py`) -- real JWTs, real pydal queries.

Fail-first proof (executed, not narrated): temporarily replaced
`services.community_authz.require_community_scope`'s body with an
unconditional `return CommunityRole(name="community-admin", ...)` --
simulating a port that gates `listRequests`/`approveRequest`/
`rejectRequest` with nothing beyond `@tenant_middleware`, i.e. "any
authenticated user can approve any community's join requests" -- the
"missing authz on admin join-request approval" class flagged for this
port. Both `test_by_non_member_of_this_community_is_403` (list) and
`test_approve_by_non_member_of_this_community_is_403` went red (200, an
outsider listed/approved another community's join requests -- the approve
case would have silently created a `community_members` row for the
applicant); reverted, green again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.join_request import join_request_bp
from config import HubAPIConfig
from services import dsar_rate_limit, join_request_service
from services import suppression_keys as keys
from services.membership_state import MembershipState
from tests.community_ids import install_community_ids, public_id
from tests.conftest import TENANT_SLUG, make_user_token
from tests.entity_ids import pid


def _review_url(auth_db: Any, community_id: int, request_id: int, verb: str) -> str:
    """`PUT .../join-requests/<request UUID>/<verb>` for a seeded request row."""
    request_uuid = pid(auth_db.dal, "community_join_requests", request_id)
    return f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests/{request_uuid}/{verb}"


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8206,
        grpc_port=50206,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        default_tenant_slug=DEFAULT_TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8206",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    install_community_ids(quart_app, auth_db.dal)
    quart_app.register_blueprint(join_request_bp)
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(auth_db: Any, *, email: str, username: str) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email,
        username=username,
        password_hash="x",
        is_active=True,
        email_verified=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


def _seed_community(auth_db: Any, *, join_mode: str = "approval") -> int:
    community_id: int = auth_db.dal.communities.insert(
        name=f"acme-{uuid.uuid4()}",
        display_name="Acme",
        is_active=True,
        is_public=True,
        join_mode=join_mode,
        config={},
    )
    auth_db.dal.commit()
    return community_id


def _seed_role(auth_db: Any, *, community_id: int, name: str, scopes: list[str]) -> int:
    kind = {"community-admin": 1, "community-owner": 1, "moderator": 2}.get(name, 0)
    role_id: int = auth_db.dal.community_roles.insert(
        community_id=community_id,
        name=name,
        is_system=kind != 0,
        role_kind=kind,
        priority=0,
        base_claims={"scopes": scopes},
    )
    auth_db.dal.commit()
    return role_id


def _seed_member(auth_db: Any, *, community_id: int, user_id: int, role_id: int) -> None:
    auth_db.dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        is_active=True,
        joined_at=datetime.now(UTC),
        community_role_id=role_id,
    )
    auth_db.dal.commit()


def _seed_admin(auth_db: Any, *, community_id: int, email: str, username: str) -> int:
    admin_id = _seed_user(auth_db, email=email, username=username)
    role_id = _seed_role(
        auth_db,
        community_id=community_id,
        name="community-admin",
        scopes=["community:manage_members", "community:manage_channels"],
    )
    _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)
    return admin_id


def _auth_header(*, user_id: int) -> dict[str, str]:
    token = make_user_token(user_id=user_id, tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


def _seed_tenant_community(auth_db: Any, *, join_mode: str = "approval") -> int:
    """Seed a community inside the caller's own (`TENANT_SLUG`) tenant."""
    cid = _seed_community(auth_db, join_mode=join_mode)
    tenant = auth_db.dal(auth_db.dal.tenants.slug == TENANT_SLUG).select().first()
    auth_db.dal(auth_db.dal.communities.id == cid).update(tenant_id=tenant.id)
    auth_db.dal.commit()
    return cid


@pytest.fixture(autouse=True)
def _fresh_submit_limiter() -> None:
    """The in-process limiter is module-global; isolate each test's budget."""
    dsar_rate_limit.reset_local()


class TestSubmitRequest:
    async def test_missing_token_is_401(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_tenant_community(auth_db)
        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests", json={}
        )
        assert response.status_code == 401

    async def test_non_approval_community_is_400(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_tenant_community(auth_db, join_mode="open")
        user_id = _seed_user(auth_db, email="joiner@example.com", username="joiner")
        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests",
            json={"message": "let me in"},
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 400

    async def test_already_member_is_409(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_tenant_community(auth_db)
        user_id = _seed_user(auth_db, email="member@example.com", username="member")
        role_id = _seed_role(
            auth_db, community_id=community_id, name="member", scopes=["community:read"]
        )
        _seed_member(auth_db, community_id=community_id, user_id=user_id, role_id=role_id)

        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests",
            json={},
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 409

    async def test_submit_success_returns_201(self, client: Any, auth_db: Any) -> None:
        """A non-member (no membership row) can submit -- joining is for non-members."""
        community_id = _seed_tenant_community(auth_db)
        user_id = _seed_user(auth_db, email="newjoiner@example.com", username="newjoiner")
        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests",
            json={"message": "please let me in"},
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["success"] is True
        assert body["request"]["status"] == "pending"
        assert "created_at" in body["request"]

    async def test_foreign_tenant_community_is_404_and_writes_nothing(
        self, client: Any, auth_db: Any
    ) -> None:
        """regression: unscoped-routes -- submit had no tenant fence at all."""
        community_id = _seed_tenant_community(auth_db)
        other_tenant = auth_db.dal.tenants.insert(slug=f"other-{uuid.uuid4().hex[:8]}")
        auth_db.dal(auth_db.dal.communities.id == community_id).update(tenant_id=other_tenant)
        auth_db.dal.commit()
        user_id = _seed_user(auth_db, email="xt@example.com", username="xtenant")
        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests",
            json={},
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 404
        assert auth_db.dal(auth_db.dal.community_join_requests).count() == 0

    async def test_duplicate_pending_request_is_409(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_tenant_community(auth_db)
        user_id = _seed_user(auth_db, email="dup@example.com", username="dup")
        url = f"/api/v1/{public_id(auth_db, community_id)}/join-requests"
        first = await client.post(url, json={}, headers=_auth_header(user_id=user_id))
        second = await client.post(url, json={}, headers=_auth_header(user_id=user_id))
        assert (first.status_code, second.status_code) == (201, 409)
        assert auth_db.dal(auth_db.dal.community_join_requests).count() == 1

    async def test_resubmit_after_rejection_reopens_the_same_row(
        self, client: Any, auth_db: Any
    ) -> None:
        community_id = _seed_tenant_community(auth_db)
        user_id = _seed_user(auth_db, email="again@example.com", username="again")
        auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=user_id,
            status="rejected",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()
        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests",
            json={},
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 201
        assert auth_db.dal(auth_db.dal.community_join_requests).count() == 1

    async def test_deactivated_member_is_403(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_tenant_community(auth_db)
        user_id = _seed_user(auth_db, email="banned@example.com", username="banned")
        auth_db.dal.community_members.insert(
            community_id=community_id, user_id=str(user_id), is_active=False
        )
        auth_db.dal.commit()
        response = await client.post(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests",
            json={},
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 403

    async def test_rate_limit_is_429_after_budget(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db, email="spam@example.com", username="spam")
        statuses = []
        for _ in range(join_request_service.JOIN_REQUEST_LIMIT + 1):
            cid = _seed_tenant_community(auth_db)
            response = await client.post(
                f"/api/v1/{public_id(auth_db, cid)}/join-requests",
                json={},
                headers=_auth_header(user_id=user_id),
            )
            statuses.append(response.status_code)
        assert statuses[:-1] == [201] * join_request_service.JOIN_REQUEST_LIMIT
        assert statuses[-1] == 429

    async def test_rate_limit_is_per_user(self, client: Any, auth_db: Any) -> None:
        cid = _seed_tenant_community(auth_db)
        spammer = _seed_user(auth_db, email="s1@example.com", username="s1")
        other = _seed_user(auth_db, email="s2@example.com", username="s2")
        for _ in range(join_request_service.JOIN_REQUEST_LIMIT + 1):
            await client.post(
                f"/api/v1/{public_id(auth_db, cid)}/join-requests",
                json={},
                headers=_auth_header(user_id=spammer),
            )
        response = await client.post(
            f"/api/v1/{public_id(auth_db, cid)}/join-requests",
            json={},
            headers=_auth_header(user_id=other),
        )
        assert response.status_code == 201


class TestGetMyRequest:
    async def test_no_request_returns_null(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        user_id = _seed_user(auth_db, email="nobody@example.com", username="nobody")
        response = await client.get(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests/mine",
            headers=_auth_header(user_id=user_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["request"] is None

    async def test_existing_request_returned(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        user_id = _seed_user(auth_db, email="haspending@example.com", username="haspending")
        auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=user_id,
            status="pending",
            message="hi",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        response = await client.get(
            f"/api/v1/{public_id(auth_db, community_id)}/join-requests/mine",
            headers=_auth_header(user_id=user_id),
        )
        body = await response.get_json()
        assert body["request"]["status"] == "pending"
        assert body["request"]["message"] == "hi"


class TestListRequests:
    async def test_missing_token_is_401(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        response = await client.get(
            f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests"
        )
        assert response.status_code == 401

    async def test_by_non_member_of_this_community_is_403(self, client: Any, auth_db: Any) -> None:
        """SECURITY (IDOR): admin of a DIFFERENT community can't list this one's requests."""
        community_id = _seed_community(auth_db)
        other_community_id = _seed_community(auth_db)
        outsider_id = _seed_admin(
            auth_db, community_id=other_community_id, email="outsider@example.com", username="out"
        )

        response = await client.get(
            f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests",
            headers=_auth_header(user_id=outsider_id),
        )
        assert response.status_code == 403

    async def test_broad_moderator_scopes_cannot_list_requests(
        self, client: Any, auth_db: Any
    ) -> None:
        """`community:manage_members` is not the dedicated `community.members:manage` scope."""
        community_id = _seed_community(auth_db)
        mod_id = _seed_user(auth_db, email="modjr@example.com", username="modjr")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="moderator",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=mod_id, role_id=role_id)
        response = await client.get(
            f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests",
            headers=_auth_header(user_id=mod_id),
        )
        assert response.status_code == 403

    async def test_admin_lists_pending_requests(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="listadmin@example.com", username="listadmin"
        )
        applicant_id = _seed_user(auth_db, email="applicant@example.com", username="applicant")
        auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=applicant_id,
            status="pending",
            message="pick me",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        response = await client.get(
            f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests",
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["total"] == 1
        assert body["requests"][0]["username"] == "applicant"
        assert body["requests"][0]["message"] == "pick me"
        assert "created_at" in body["requests"][0]


class TestApproveReject:
    async def test_approve_by_non_member_of_this_community_is_403(
        self, client: Any, auth_db: Any
    ) -> None:
        """SECURITY: proves `require_community_scope` gates this route.

        See this module's fail-first docstring.
        """
        community_id = _seed_community(auth_db)
        other_community_id = _seed_community(auth_db)
        outsider_id = _seed_admin(
            auth_db,
            community_id=other_community_id,
            email="outsider2@example.com",
            username="outsider2",
        )
        applicant_id = _seed_user(auth_db, email="target@example.com", username="target")
        request_id = auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=applicant_id,
            status="pending",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "approve"),
            headers=_auth_header(user_id=outsider_id),
        )
        assert response.status_code == 403

    async def test_approve_success_creates_membership(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="approver@example.com", username="approver"
        )
        applicant_id = _seed_user(auth_db, email="joins@example.com", username="joins")
        request_id = auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=applicant_id,
            status="pending",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "approve"),
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 200

        member_rows = await auth_db.select_async(
            auth_db.dal(
                (auth_db.dal.community_members.community_id == community_id)
                & (auth_db.dal.community_members.user_id == str(applicant_id))
            )
        )
        assert len(member_rows) == 1
        # Regression (PR #1542 review): the approved joiner LINKS the seeded `member` system role.
        roles = auth_db.dal.community_roles
        member_role = (
            auth_db.dal(
                (roles.community_id == community_id)
                & (roles.name == "member")
                & (roles.is_system == True)  # noqa: E712
            )
            .select()
            .first()
        )
        assert member_role is not None
        assert member_rows.first().community_role_id == member_role.id
        req_rows = await auth_db.select_async(
            auth_db.dal(auth_db.dal.community_join_requests.id == request_id)
        )
        assert req_rows.first().status == "approved"
        assert req_rows.first().reviewed_by == admin_id

    @staticmethod
    def _suppress(auth_db: Any, *, tenant_id: int, platform_user_id: str) -> None:
        """Seed a live `suppression_records` row exactly as erasure would have written it."""
        code = keys.current_code(
            keys.PlatformKekKeySource(), tenant_id, "discord", platform_user_id
        )
        auth_db.dal.suppression_records.insert(
            tenant_id=tenant_id,
            platform="discord",
            key_ver=code.key_ver,
            hmac=code.hmac,
            reason_enum="ban",
            expires_at=None,
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

    @staticmethod
    def _pending(auth_db: Any, *, community_id: int, applicant_id: int) -> int:
        request_id: int = auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=applicant_id,
            status="pending",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()
        return request_id

    @staticmethod
    def _link(auth_db: Any, *, user_id: int, platform_user_id: str) -> None:
        auth_db.dal.hub_user_identities.insert(
            hub_user_id=user_id,
            platform="discord",
            platform_user_id=platform_user_id,
            linked_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

    @staticmethod
    def _tenant_id(auth_db: Any, slug: str = TENANT_SLUG) -> int:
        row = auth_db.dal(auth_db.dal.tenants.slug == slug).select().first()
        auth_db.dal.commit()
        if row is None:
            new_id = auth_db.dal.tenants.insert(slug=slug, display_name=slug, is_active=True)
            auth_db.dal.commit()
            return int(new_id)
        return int(row.id)

    async def test_approve_refuses_a_suppressed_applicant_and_reapplies_the_ban(
        self, client: Any, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A banned user who erased their account and came back is not admitted."""
        monkeypatch.setenv(keys.ENV_CURRENT, "ab" * 32)
        tenant_id = self._tenant_id(auth_db)
        community_id = _seed_community(auth_db)
        auth_db.dal(auth_db.dal.communities.id == community_id).update(tenant_id=tenant_id)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="ap-s@example.com", username="ap-s"
        )
        applicant_id = _seed_user(auth_db, email="back@example.com", username="back")
        self._link(auth_db, user_id=applicant_id, platform_user_id="d-banned-1")
        self._suppress(auth_db, tenant_id=tenant_id, platform_user_id="d-banned-1")
        request_id = self._pending(auth_db, community_id=community_id, applicant_id=applicant_id)

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "approve"),
            headers=_auth_header(user_id=admin_id),
        )

        assert response.status_code == 403
        member = (
            auth_db.dal(
                (auth_db.dal.community_members.community_id == community_id)
                & (auth_db.dal.community_members.user_id == str(applicant_id))
            )
            .select()
            .first()
        )
        request = auth_db.dal(auth_db.dal.community_join_requests.id == request_id).select().first()
        auth_db.dal.commit()
        # restriction re-applied: the ban is the BANNED state (alembic 0146), never a role string
        assert (int(member.membership_state), member.is_active) == (
            int(MembershipState.BANNED),
            False,
        )
        assert (request.status, request.reviewed_by) == ("rejected", admin_id)

    async def test_approve_ignores_a_suppression_from_another_tenant(
        self, client: Any, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(keys.ENV_CURRENT, "ab" * 32)
        tenant_id = self._tenant_id(auth_db)
        other_tenant = self._tenant_id(auth_db, "elsewhere")
        community_id = _seed_community(auth_db)
        auth_db.dal(auth_db.dal.communities.id == community_id).update(tenant_id=tenant_id)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="ap-x@example.com", username="ap-x"
        )
        applicant_id = _seed_user(auth_db, email="clean@example.com", username="clean")
        self._link(auth_db, user_id=applicant_id, platform_user_id="d-banned-2")
        self._suppress(auth_db, tenant_id=other_tenant, platform_user_id="d-banned-2")
        request_id = self._pending(auth_db, community_id=community_id, applicant_id=applicant_id)

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "approve"),
            headers=_auth_header(user_id=admin_id),
        )

        assert response.status_code == 200
        member = (
            auth_db.dal(
                (auth_db.dal.community_members.community_id == community_id)
                & (auth_db.dal.community_members.user_id == str(applicant_id))
            )
            .select()
            .first()
        )
        auth_db.dal.commit()
        linked = auth_db.dal(auth_db.dal.community_roles.id == member.community_role_id).select()
        auth_db.dal.commit()
        assert [(r.name, bool(r.is_system)) for r in linked] == [("member", True)]
        assert member.is_active is True

    async def test_approve_fails_closed_when_rows_exist_but_the_key_is_missing(
        self, client: Any, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_id = self._tenant_id(auth_db)
        community_id = _seed_community(auth_db)
        auth_db.dal(auth_db.dal.communities.id == community_id).update(tenant_id=tenant_id)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="ap-f@example.com", username="ap-f"
        )
        applicant_id = _seed_user(auth_db, email="keyless@example.com", username="keyless")
        self._link(auth_db, user_id=applicant_id, platform_user_id="d-banned-3")
        monkeypatch.setenv(keys.ENV_CURRENT, "ab" * 32)
        self._suppress(auth_db, tenant_id=tenant_id, platform_user_id="d-banned-3")
        monkeypatch.delenv(keys.ENV_CURRENT)
        request_id = self._pending(auth_db, community_id=community_id, applicant_id=applicant_id)

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "approve"),
            headers=_auth_header(user_id=admin_id),
        )

        assert response.status_code == 500  # never admits a possibly-banned user
        members = auth_db.dal(
            (auth_db.dal.community_members.community_id == community_id)
            & (auth_db.dal.community_members.user_id == str(applicant_id))
        ).count()
        request = auth_db.dal(auth_db.dal.community_join_requests.id == request_id).select().first()
        auth_db.dal.commit()
        assert members == 0
        assert request.status == "pending"  # retryable once the key is fixed

    async def test_foreign_community_request_uuid_is_the_same_404_as_unknown(
        self, client: Any, auth_db: Any
    ) -> None:
        """A request UUID from ANOTHER community never resolves under this community's URL."""
        community_id = _seed_community(auth_db)
        other_community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="adminuuid@example.com", username="adminuuid"
        )
        applicant_id = _seed_user(auth_db, email="foreign@example.com", username="foreignapp")
        foreign_request = self._pending(
            auth_db, community_id=other_community_id, applicant_id=applicant_id
        )
        base = f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests"
        foreign_uuid = pid(auth_db.dal, "community_join_requests", foreign_request)
        unknown_uuid = "00000000-0000-4000-8000-0000000000ff"
        for verb in ("approve", "reject"):
            foreign = await client.put(
                f"{base}/{foreign_uuid}/{verb}", headers=_auth_header(user_id=admin_id)
            )
            unknown = await client.put(
                f"{base}/{unknown_uuid}/{verb}", headers=_auth_header(user_id=admin_id)
            )
            assert foreign.status_code == unknown.status_code == 404, verb
            bodies = [await foreign.get_json(), await unknown.get_json()]
            for body in bodies:
                body["error"].pop("timestamp", None)
            assert bodies[0] == bodies[1], verb
        row = (
            auth_db.dal(auth_db.dal.community_join_requests.id == foreign_request).select().first()
        )
        assert row.status == "pending"  # the other community's request was never touched

    async def test_numeric_request_id_in_the_path_is_404(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="adminnum@example.com", username="adminnum"
        )
        request_id = self._pending(
            auth_db,
            community_id=community_id,
            applicant_id=_seed_user(auth_db, email="num@example.com", username="numapp"),
        )
        for verb in ("approve", "reject"):
            response = await client.put(
                f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests/{request_id}/{verb}",  # noqa: E501
                headers=_auth_header(user_id=admin_id),
            )
            assert response.status_code == 404, verb

    async def test_approve_no_pending_request_is_404(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="approver2@example.com", username="approver2"
        )
        response = await client.put(
            f"/api/v1/admin/{public_id(auth_db, community_id)}/join-requests/00000000-0000-4000-8000-0000000000ff/approve",  # noqa: E501
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 404

    async def test_reject_success(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="rejecter@example.com", username="rejecter"
        )
        applicant_id = _seed_user(auth_db, email="rejected@example.com", username="rejected")
        request_id = auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=applicant_id,
            status="pending",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "reject"),
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 200

        req_rows = await auth_db.select_async(
            auth_db.dal(auth_db.dal.community_join_requests.id == request_id)
        )
        assert req_rows.first().status == "rejected"

    async def test_reject_already_reviewed_is_404(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_admin(
            auth_db, community_id=community_id, email="rejecter2@example.com", username="rejecter2"
        )
        applicant_id = _seed_user(auth_db, email="alreadydone@example.com", username="alreadydone")
        request_id = auth_db.dal.community_join_requests.insert(
            community_id=community_id,
            user_id=applicant_id,
            status="approved",
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        response = await client.put(
            _review_url(auth_db, community_id, request_id, "reject"),
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 404
