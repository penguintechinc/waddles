"""`POST /api/v1/communities/<id>/join` -- open join, closed 403, idempotent, tenancy, 401."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_join import community_join_bp
from tests.conftest import TENANT_SLUG, make_user_token
from tests.test_v1_join_request_blueprint import (
    _auth_header,
    _seed_user,
    _test_config,
)
from tests.test_v1_join_request_blueprint import (
    _seed_community as _seed_bare_community,
)


def _seed_community(auth_db: Any, *, join_mode: str) -> int:
    """Seed a community in the caller's own tenant (the `acme-corp` tenant row)."""
    cid = _seed_bare_community(auth_db, join_mode=join_mode)
    tenant = auth_db.dal(auth_db.dal.tenants.slug == TENANT_SLUG).select().first()
    auth_db.dal(auth_db.dal.communities.id == cid).update(tenant_id=tenant.id)
    auth_db.dal.commit()
    return cid


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(community_join_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _members(auth_db: Any, community_id: int, user_id: int) -> int:
    return int(
        auth_db.dal(
            (auth_db.dal.community_members.community_id == community_id)
            & (auth_db.dal.community_members.user_id == str(user_id))
        ).count()
    )


class TestJoinCommunity:
    async def test_unauthenticated_is_401(self, client: Any, auth_db: Any) -> None:
        cid = _seed_community(auth_db, join_mode="open")
        response = await client.post(f"/api/v1/communities/{cid}/join")
        assert response.status_code == 401

    async def test_open_join_creates_membership_with_exact_fields(
        self, client: Any, auth_db: Any
    ) -> None:
        cid = _seed_community(auth_db, join_mode="open")
        uid = _seed_user(auth_db, email="o@example.com", username="opener")
        response = await client.post(
            f"/api/v1/communities/{cid}/join", headers=_auth_header(user_id=uid)
        )
        assert response.status_code == 201
        assert await response.get_json() == {"success": True, "community_id": cid, "joined": True}
        assert _members(auth_db, cid, uid) == 1

    async def test_join_is_idempotent(self, client: Any, auth_db: Any) -> None:
        cid = _seed_community(auth_db, join_mode="open")
        uid = _seed_user(auth_db, email="i@example.com", username="idem")
        headers = _auth_header(user_id=uid)
        first = await client.post(f"/api/v1/communities/{cid}/join", headers=headers)
        second = await client.post(f"/api/v1/communities/{cid}/join", headers=headers)
        assert first.status_code == 201
        assert second.status_code == 200
        assert (await second.get_json())["joined"] is False
        assert _members(auth_db, cid, uid) == 1

    @pytest.mark.parametrize("mode", ["approval", "invite"])
    async def test_closed_community_is_403_typed(
        self, client: Any, auth_db: Any, mode: str
    ) -> None:
        cid = _seed_community(auth_db, join_mode=mode)
        uid = _seed_user(auth_db, email=f"{mode}@example.com", username=mode)
        response = await client.post(
            f"/api/v1/communities/{cid}/join", headers=_auth_header(user_id=uid)
        )
        assert response.status_code == 403
        assert "JOIN_NOT_OPEN" in str(await response.get_json())
        assert _members(auth_db, cid, uid) == 0

    async def test_inactive_member_cannot_rejoin(self, client: Any, auth_db: Any) -> None:
        cid = _seed_community(auth_db, join_mode="open")
        uid = _seed_user(auth_db, email="b@example.com", username="banned")
        auth_db.dal.community_members.insert(
            community_id=cid,
            user_id=str(uid),
            role="member",
            is_active=False,
            joined_at=datetime.now(UTC),
        )
        auth_db.dal.commit()
        response = await client.post(
            f"/api/v1/communities/{cid}/join", headers=_auth_header(user_id=uid)
        )
        assert response.status_code == 403

    async def test_cross_tenant_community_is_404(self, client: Any, auth_db: Any) -> None:
        cid = _seed_community(auth_db, join_mode="open")
        other_tenant = auth_db.dal.tenants.insert(slug=f"other-{uuid.uuid4().hex[:8]}")
        auth_db.dal(auth_db.dal.communities.id == cid).update(tenant_id=other_tenant)
        auth_db.dal.commit()
        uid = _seed_user(auth_db, email="x@example.com", username="xtenant")
        token = make_user_token(user_id=uid, tenant=TENANT_SLUG)
        response = await client.post(
            f"/api/v1/communities/{cid}/join", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 404
        assert _members(auth_db, cid, uid) == 0
