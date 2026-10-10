"""`blueprints/v1/platform.py` -- the M3 Platform-admin group (platformController.js).

Standalone Quart app registering only `platform_bp`, matching
`test_platform_blueprint.py`'s (v2) pattern -- real JWTs via
`flask_core.auth.create_jwt_token`, real pydal queries against the
`platform_db` fixture (`tests/conftest.py`).

Fail-first proof (executed, not narrated): temporarily swapped
`require_scope("platform:admin")` for `require_scope("platform:read")`
on `list_users`' decorator chain -- `test_list_users_wrong_scope_is_403`
went red (200 instead of 403, since the admin-bundle token in
`_admin_headers` no longer matched the swapped-in scope); reverted,
green again. Separately, temporarily made `list_communities` skip its
`is_active` filter entirely -- `test_list_communities_filters_inactive`
went red (an inactive-seeded community leaked into the response);
reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.platform import platform_bp
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
def app(platform_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(platform_bp)
    quart_app.config["dal"] = platform_db.dal
    quart_app.config["async_dal"] = platform_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _admin_headers(*, user_id: int = 1) -> dict[str, str]:
    # global:admin bundle -- SCOPE_BUNDLES["global"]["admin"] includes the
    # `*:admin` wildcard, which flask_core.authz._scope_covers's own rule
    # (resource `*` + exact action match) satisfies `platform:admin`.
    token = make_user_token(
        user_id=user_id,
        scope="*:read *:write *:admin *:delete settings:write users:admin",
        tenant=TENANT_SLUG,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_member(
    platform_db: Any,
    *,
    community_id: int,
    user_id: str,
    platform: str = "twitch",
    display_name: str = "Alice",
    is_active: bool = True,
) -> int:
    row_id: int = platform_db.dal.community_members.insert(
        community_id=community_id,
        user_id=user_id,
        platform=platform,
        platform_user_id=f"pu-{user_id}",
        display_name=display_name,
        role="member",
        is_active=is_active,
        joined_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    platform_db.dal.commit()
    return row_id


def _seed_community(
    platform_db: Any, *, name: str = "acme", is_active: bool = True, is_public: bool = True
) -> int:
    row_id: int = platform_db.dal.communities.insert(
        name=name,
        display_name=name.title(),
        is_active=is_active,
        is_public=is_public,
        member_count=1,
        tenant_id=1,
        created_at=datetime.now(UTC),
    )
    platform_db.dal.commit()
    return row_id


class TestScopeEnforcement:
    async def test_list_users_no_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/users")
        assert response.status_code == 401

    async def test_list_users_wrong_scope_is_403(self, client: Any, auth_headers: Any) -> None:
        """The representative scope-check: a valid token WITHOUT platform:admin is refused."""
        response = await client.get(
            "/api/v1/platform/users", headers=auth_headers(scope="platform:read")
        )
        assert response.status_code == 403

    async def test_list_users_with_admin_scope_returns_200(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/users", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True


class TestUsers:
    async def test_list_users_returns_seeded_member(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        _seed_member(platform_db, community_id=community_id, user_id="42")

        response = await client.get("/api/v1/platform/users", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["total"] == 1
        assert body["users"][0]["userId"] == "42"

    async def test_list_users_search_and_platform_filters(
        self, client: Any, platform_db: Any
    ) -> None:
        community_id = _seed_community(platform_db)
        _seed_member(
            platform_db,
            community_id=community_id,
            user_id="1",
            platform="twitch",
            display_name="Alice",
        )
        _seed_member(
            platform_db,
            community_id=community_id,
            user_id="2",
            platform="discord",
            display_name="Bob",
        )

        by_search = await client.get(
            "/api/v1/platform/users",
            headers=_admin_headers(),
            query_string={"search": "Alice"},
        )
        body = await by_search.get_json()
        assert [u["userId"] for u in body["users"]] == ["1"]

        by_platform = await client.get(
            "/api/v1/platform/users",
            headers=_admin_headers(),
            query_string={"platform": "discord"},
        )
        body2 = await by_platform.get_json()
        assert [u["userId"] for u in body2["users"]] == ["2"]

    async def test_get_user_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/users/9999", headers=_admin_headers())
        assert response.status_code == 404

    async def test_get_user_found_with_memberships(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        _seed_member(platform_db, community_id=community_id, user_id="7")

        response = await client.get("/api/v1/platform/users/7", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["user"]["userId"] == "7"
        assert body["user"]["isPlatformAdmin"] is False
        assert len(body["user"]["memberships"]) == 1

    async def test_update_user_role_grants_platform_admin(
        self, client: Any, platform_db: Any
    ) -> None:
        response = await client.put(
            "/api/v1/platform/users/7/role",
            headers=_admin_headers(),
            json={"role": "platform-admin"},
        )
        assert response.status_code == 200
        # Same-connection read -- AsyncDAL.insert_async()/update_async()
        # never commit(); a bare synchronous platform_db.dal(...) query is
        # a DIFFERENT connection and would see the pre-write state (see
        # hub_api/PORTING.md Gotcha #2's third related gotcha).
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.platform_admins.user_id == 7)
        )
        row = rows.first()
        assert row is not None
        assert row.role == "platform-admin"
        assert row.is_active is True

    async def test_update_user_role_invalid_role_is_400(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/platform/users/7/role", headers=_admin_headers(), json={"role": "bogus"}
        )
        assert response.status_code == 400

    async def test_update_user_role_re_grant_updates_existing_row(
        self, client: Any, platform_db: Any
    ) -> None:
        """Second grant call hits the UPDATE-existing-row branch, not the INSERT branch."""
        first = await client.put(
            "/api/v1/platform/users/7/role",
            headers=_admin_headers(),
            json={"role": "support"},
        )
        assert first.status_code == 200
        second = await client.put(
            "/api/v1/platform/users/7/role",
            headers=_admin_headers(),
            json={"role": "platform-admin"},
        )
        assert second.status_code == 200
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.platform_admins.user_id == 7)
        )
        assert len(rows) == 1
        assert rows.first().role == "platform-admin"

    async def test_update_user_role_clear_deactivates(self, client: Any, platform_db: Any) -> None:
        grant = await client.put(
            "/api/v1/platform/users/7/role",
            headers=_admin_headers(),
            json={"role": "platform-admin"},
        )
        assert grant.status_code == 200
        clear = await client.put(
            "/api/v1/platform/users/7/role", headers=_admin_headers(), json={"role": None}
        )
        assert clear.status_code == 200
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.platform_admins.user_id == 7)
        )
        assert rows.first().is_active is False

    async def test_deactivate_user(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        _seed_member(platform_db, community_id=community_id, user_id="7")

        response = await client.delete(
            "/api/v1/platform/users/7", headers=_admin_headers(), json={"reason": "spam"}
        )
        assert response.status_code == 200
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.community_members.user_id == "7")
        )
        assert rows.first().is_active is False


class TestCommunities:
    async def test_list_communities_filters_inactive(self, client: Any, platform_db: Any) -> None:
        _seed_community(platform_db, name="active-one", is_active=True)
        _seed_community(platform_db, name="inactive-one", is_active=False)

        response = await client.get("/api/v1/platform/communities", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        names = [c["name"] for c in body["communities"]]
        assert "active-one" in names
        assert "inactive-one" not in names

    async def test_list_communities_excludes_tenant_wide_sentinel(
        self, client: Any, platform_db: Any
    ) -> None:
        """The sentinel community (id 0, grants seeder) never appears, even for isActive=false."""
        platform_db.dal.communities.insert(
            id=0,
            name="__tenant_wide__",
            is_active=False,
            is_public=False,
            member_count=0,
            tenant_id=1,
            created_at=datetime.now(UTC),
        )
        platform_db.dal.commit()
        for query in ("", "?isActive=false"):
            response = await client.get(
                f"/api/v1/platform/communities{query}", headers=_admin_headers()
            )
            assert response.status_code == 200
            body = await response.get_json()
            assert "__tenant_wide__" not in [c["name"] for c in body["communities"]]

    async def test_list_communities_search(self, client: Any, platform_db: Any) -> None:
        _seed_community(platform_db, name="findme")
        _seed_community(platform_db, name="other")
        response = await client.get(
            "/api/v1/platform/communities",
            headers=_admin_headers(),
            query_string={"search": "find"},
        )
        body = await response.get_json()
        assert [c["name"] for c in body["communities"]] == ["findme"]

    async def test_get_community_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/communities/9999", headers=_admin_headers())
        assert response.status_code == 404

    async def test_get_community_found(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        response = await client.get(
            f"/api/v1/platform/communities/{community_id}", headers=_admin_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["community"]["id"] == community_id
        assert body["community"]["owner"] is None

    async def test_update_community(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        response = await client.put(
            f"/api/v1/platform/communities/{community_id}",
            headers=_admin_headers(),
            json={
                "displayName": "New Name",
                "description": "new desc",
                "isPublic": False,
                "isActive": False,
            },
        )
        assert response.status_code == 200
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.communities.id == community_id)
        )
        row = rows.first()
        assert row.display_name == "New Name"
        assert row.description == "new desc"
        assert row.is_public is False
        assert row.is_active is False

    async def test_update_community_no_fields_is_400(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        response = await client.put(
            f"/api/v1/platform/communities/{community_id}", headers=_admin_headers(), json={}
        )
        assert response.status_code == 400

    async def test_update_community_not_found_is_404(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/platform/communities/9999",
            headers=_admin_headers(),
            json={"displayName": "x"},
        )
        assert response.status_code == 404

    async def test_deactivate_community_not_found_is_404(self, client: Any) -> None:
        response = await client.delete(
            "/api/v1/platform/communities/9999", headers=_admin_headers(), json={}
        )
        assert response.status_code == 404

    async def test_deactivate_community(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        response = await client.delete(
            f"/api/v1/platform/communities/{community_id}",
            headers=_admin_headers(),
            json={"reason": "tos violation"},
        )
        assert response.status_code == 200
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.communities.id == community_id)
        )
        assert rows.first().is_active is False


class TestSystem:
    async def test_health(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/health", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "healthy"
        assert body["checks"]["database"] is True

    async def test_modules_empty(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/modules", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["modules"] == []

    async def test_audit_log_empty(self, client: Any) -> None:
        response = await client.get("/api/v1/platform/audit-log", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["entries"] == []
        assert body["pagination"]["total"] == 0

    async def test_audit_log_filters_by_action_and_user(
        self, client: Any, platform_db: Any
    ) -> None:
        platform_db.dal.audit_log.insert(user_id=7, action="login", created_at=datetime.now(UTC))
        platform_db.dal.audit_log.insert(user_id=8, action="logout", created_at=datetime.now(UTC))
        platform_db.dal.commit()

        by_action = await client.get(
            "/api/v1/platform/audit-log",
            headers=_admin_headers(),
            query_string={"action": "login"},
        )
        body = await by_action.get_json()
        assert len(body["entries"]) == 1
        assert body["entries"][0]["action"] == "login"

        by_user = await client.get(
            "/api/v1/platform/audit-log",
            headers=_admin_headers(),
            query_string={"userId": "8"},
        )
        body2 = await by_user.get_json()
        assert len(body2["entries"]) == 1
        assert body2["entries"][0]["userId"] == 8

    async def test_stats(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db)
        _seed_member(platform_db, community_id=community_id, user_id="7")

        response = await client.get("/api/v1/platform/stats", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stats"]["communities"]["total"] == 1
        assert body["stats"]["users"]["total"] == 1
