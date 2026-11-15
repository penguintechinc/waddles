"""
Community-Scoped Authorization Tests
======================================

Covers `community_access.install_community_scoped_auth` -- the shared
BOLA/IDOR fix for `community_id` path parameters (security_core_module's
own confirmed vulnerability: 10 unauthenticated routes, several taking
`community_id` straight off the URL with zero membership check). Also
covers `require_admin`/`require_member`/`decode_caller_user_id` directly.

Fail-first proof: with `install_community_scoped_auth`'s bearer-token check
temporarily replaced with a bare `return None` (no auth at all -- the
original vulnerable shape), `test_no_token_is_401`,
`test_non_member_is_403_on_get`, and `test_member_forbidden_from_admin_route`
all went green->red as expected (200 instead of 401/403); reverted after
confirming. See PR report for the exact before/after run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from pydal import DAL
from quart import Blueprint, Quart

from flask_core.auth import create_jwt_token
from flask_core.community_access import (
    CallerIdentityError,
    CommunityAccessError,
    bind_shared_read_tables,
    decode_caller_user_id,
    install_community_scoped_auth,
    require_admin,
    require_member,
)
from flask_core.tenancy import TenantContext

SECRET = "change-me-in-production"


def _token(*, sub: str = "1", tenant: str = "acme-corp", roles: list[str] | None = None) -> str:
    return create_jwt_token(
        user_id=sub,
        username="alice",
        email="alice@example.com",
        roles=roles or [],
        tenant=tenant,
    )


@pytest.fixture
def db() -> Any:
    dal = DAL("sqlite:memory")
    bind_shared_read_tables(dal, migrate=True)
    yield dal
    dal.close()


@pytest.fixture
def seeded(db: Any) -> dict[str, int]:
    """One tenant, one community in it, a member and a non-member row."""
    tenant_id = db.tenants.insert(slug="acme-corp", is_active=True)
    other_tenant_id = db.tenants.insert(slug="other-corp", is_active=True)
    community_id = db.communities.insert(tenant_id=tenant_id)
    other_tenant_community_id = db.communities.insert(tenant_id=other_tenant_id)
    db.community_members.insert(
        community_id=community_id, user_id="1", role="member", is_active=True
    )
    db.community_members.insert(
        community_id=community_id, user_id="2", role="community-admin", is_active=True
    )
    db.commit()
    return {
        "tenant_id": tenant_id,
        "community_id": community_id,
        "other_tenant_community_id": other_tenant_community_id,
    }


class _FakeAsyncDAL:
    """Runs pydal calls synchronously -- fine for a sqlite:memory unit test."""

    def __init__(self, dal: Any) -> None:
        self.dal = dal

    async def select_async(self, query_set: Any, *fields: Any) -> Any:
        return query_set.select(*fields) if fields else query_set.select()


@pytest.fixture
def async_dal(db: Any) -> _FakeAsyncDAL:
    return _FakeAsyncDAL(db)


class TestRequireAdminAndMember:
    async def test_member_passes_require_member(
        self, db: Any, async_dal: _FakeAsyncDAL, seeded: dict[str, int]
    ) -> None:
        ctx = TenantContext(tenant_id=seeded["tenant_id"], tenant_slug="acme-corp")
        await require_member(
            async_dal, db, request=_FakeRequest(), ctx=ctx,
            community_id=seeded["community_id"], user_id=1,
        )

    async def test_non_member_fails_require_member(
        self, db: Any, async_dal: _FakeAsyncDAL, seeded: dict[str, int]
    ) -> None:
        ctx = TenantContext(tenant_id=seeded["tenant_id"], tenant_slug="acme-corp")
        with pytest.raises(CommunityAccessError):
            await require_member(
                async_dal, db, request=_FakeRequest(), ctx=ctx,
                community_id=seeded["community_id"], user_id=999,
            )

    async def test_member_fails_require_admin(
        self, db: Any, async_dal: _FakeAsyncDAL, seeded: dict[str, int]
    ) -> None:
        """A plain member (role='member') is not admin-tier."""
        ctx = TenantContext(tenant_id=seeded["tenant_id"], tenant_slug="acme-corp")
        with pytest.raises(CommunityAccessError):
            await require_admin(
                async_dal, db, request=_FakeRequest(), ctx=ctx,
                community_id=seeded["community_id"], user_id=1,
            )

    async def test_admin_passes_require_admin(
        self, db: Any, async_dal: _FakeAsyncDAL, seeded: dict[str, int]
    ) -> None:
        ctx = TenantContext(tenant_id=seeded["tenant_id"], tenant_slug="acme-corp")
        await require_admin(
            async_dal, db, request=_FakeRequest(), ctx=ctx,
            community_id=seeded["community_id"], user_id=2,
        )

    async def test_cross_tenant_community_id_is_denied(
        self, db: Any, async_dal: _FakeAsyncDAL, seeded: dict[str, int]
    ) -> None:
        """The IDOR case: a real `community_id`, just not in the caller's tenant."""
        ctx = TenantContext(tenant_id=seeded["tenant_id"], tenant_slug="acme-corp")
        with pytest.raises(CommunityAccessError):
            await require_member(
                async_dal, db, request=_FakeRequest(), ctx=ctx,
                community_id=seeded["other_tenant_community_id"], user_id=1,
            )


class _FakeRequest:
    """Minimal stand-in for quart.Request -- no Authorization header (non-super-admin path)."""

    headers: ClassVar[dict[str, str]] = {}


class TestDecodeCallerUserId:
    def test_no_header_raises(self) -> None:
        with pytest.raises(CallerIdentityError):
            decode_caller_user_id(_FakeRequest())


# ---------------------------------------------------------------------------
# Full-stack: install_community_scoped_auth wired onto a real Quart blueprint
# ---------------------------------------------------------------------------


@pytest.fixture
def app(db: Any) -> Quart:
    bp = Blueprint("communities", __name__, url_prefix="/api/v1/communities")
    install_community_scoped_auth(bp)

    @bp.route("/<int:community_id>/status", methods=["GET"])
    async def get_status(community_id: int) -> tuple[dict[str, Any], int]:
        return {"community_id": community_id}, 200

    @bp.route("/<int:community_id>/config", methods=["PUT"])
    async def set_config(community_id: int) -> tuple[dict[str, Any], int]:
        return {"community_id": community_id}, 200

    @bp.route("/whoami", methods=["GET"])
    async def whoami() -> tuple[dict[str, Any], int]:
        """No community_id param -- only tenant/token auth applies."""
        return {"ok": True}, 200

    quart_app = Quart(__name__)
    quart_app.config["dal"] = db
    quart_app.config["async_dal"] = _FakeAsyncDAL(db)
    quart_app.register_blueprint(bp)
    return quart_app


@pytest_asyncio.fixture
async def client(app: Quart) -> AsyncIterator[Any]:
    yield app.test_client()


class TestInstallCommunityScopedAuth:
    async def test_no_token_is_401(self, client: Any, seeded: dict[str, int]) -> None:
        response = await client.get(f"/api/v1/communities/{seeded['community_id']}/status")
        assert response.status_code == 401

    async def test_invalid_token_is_401(self, client: Any, seeded: dict[str, int]) -> None:
        response = await client.get(
            f"/api/v1/communities/{seeded['community_id']}/status",
            headers={"Authorization": "Bearer garbage"},
        )
        assert response.status_code == 401

    async def test_non_member_is_403_on_get(self, client: Any, seeded: dict[str, int]) -> None:
        token = _token(sub="999")
        response = await client.get(
            f"/api/v1/communities/{seeded['community_id']}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_member_passes_read_route(self, client: Any, seeded: dict[str, int]) -> None:
        token = _token(sub="1")
        response = await client.get(
            f"/api/v1/communities/{seeded['community_id']}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    async def test_member_forbidden_from_admin_route(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        """A plain member can GET status but cannot PUT config -- write requires admin."""
        token = _token(sub="1")
        response = await client.put(
            f"/api/v1/communities/{seeded['community_id']}/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_admin_passes_write_route(self, client: Any, seeded: dict[str, int]) -> None:
        token = _token(sub="2")
        response = await client.put(
            f"/api/v1/communities/{seeded['community_id']}/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    async def test_cross_tenant_community_id_is_403(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        """Same shape as a same-numbered community in another tenant -- 403, not a leak."""
        token = _token(sub="1")
        response = await client.get(
            f"/api/v1/communities/{seeded['other_tenant_community_id']}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_route_without_community_id_only_needs_valid_token(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        token = _token(sub="1")
        response = await client.get(
            "/api/v1/communities/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200


@pytest.fixture
def app_level_app(db: Any) -> Quart:
    """`install_community_scoped_auth` applied directly to a `Quart` app, not a `Blueprint`.

    Covers services (e.g. `core/engagement_module`) that register routes
    on `app` directly rather than via a `Blueprint` -- `/health` must stay
    reachable with no token via `exempt_paths`.
    """
    quart_app = Quart(__name__)
    quart_app.config["dal"] = db
    quart_app.config["async_dal"] = _FakeAsyncDAL(db)
    install_community_scoped_auth(quart_app, exempt_paths=frozenset({"/health"}))

    @quart_app.route("/health")
    async def health() -> tuple[dict[str, Any], int]:
        return {"status": "healthy"}, 200

    @quart_app.route("/api/v1/widgets", methods=["GET"])
    async def list_widgets() -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    return quart_app


class TestInstallCommunityScopedAuthOnApp:
    async def test_exempt_path_needs_no_token(self, app_level_app: Quart) -> None:
        client = app_level_app.test_client()
        response = await client.get("/health")
        assert response.status_code == 200

    async def test_non_exempt_route_requires_token(self, app_level_app: Quart) -> None:
        client = app_level_app.test_client()
        response = await client.get("/api/v1/widgets")
        assert response.status_code == 401

    async def test_non_exempt_route_passes_with_valid_token(
        self, app_level_app: Quart, db: Any
    ) -> None:
        db.tenants.insert(slug="acme-corp", is_active=True)
        db.commit()
        client = app_level_app.test_client()
        token = _token(sub="1")
        response = await client.get(
            "/api/v1/widgets", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
