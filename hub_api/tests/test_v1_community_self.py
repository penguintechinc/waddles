"""Self-create (`POST /api/v1/communities`) and activity (`GET .../activity`) route tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_self as mod
from services import free_tier_caps
from services.scope_registry import KIND_ADMIN
from tests.community_ids import cpath, install_community_ids
from tests.conftest import (
    TENANT_SLUG,
    make_user_token,
    seed_community,
    seed_membership,
)

CALLER = 7
CREATE_SCOPE = "communities:create"
ITEM_KEYS = {"kind", "at", "actorId", "actorName", "title"}


@pytest.fixture
def flag(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the PostHog flag (default ON) and the free-tier cap lookup (no license server)."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "feature_enabled", stub)
    monkeypatch.setattr(free_tier_caps, "_caps_apply", AsyncMock(return_value=False))
    return stub


@pytest.fixture
def app(automation_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = automation_db.dal
    quart_app.config["async_dal"] = automation_db
    install_community_ids(quart_app, automation_db.dal)
    quart_app.register_blueprint(mod.community_self_bp)
    return quart_app


def _hdr(user_id: int = CALLER, scope: str = CREATE_SCOPE) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope)}"}


def _users(db: Any, n: int = 3) -> None:
    for i in range(n):
        db.dal.hub_users.insert(username=f"user{i + 1}")
    db.dal.commit()


class TestSelfCreate:
    async def test_flag_off_is_403(self, app: Quart, flag: AsyncMock) -> None:
        flag.return_value = False
        resp = await app.test_client().post(
            "/api/v1/communities", json={"name": "x"}, headers=_hdr()
        )
        assert resp.status_code == 403
        assert "feature_disabled" in (await resp.get_data(as_text=True))

    async def test_flag_on_creates_with_admin_role_in_caller_tenant(
        self, app: Quart, automation_db: Any, flag: AsyncMock
    ) -> None:
        dal = automation_db.dal
        tenant = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        resp = await app.test_client().post(
            "/api/v1/communities",
            json={"name": "My Place", "tenant": "evil", "tenantId": 999},
            headers=_hdr(),
        )
        assert resp.status_code == 201
        body = await resp.get_json()
        assert set(body["community"]) == {"id", "name", "displayName"}
        row = dal(dal.communities.name == "my-place").select().first()
        assert int(row.tenant_id) == int(tenant.id)
        member = dal(dal.community_members.community_id == row.id).select().first()
        assert member.user_id == str(CALLER) and member.is_active
        role = dal(dal.community_roles.id == member.community_role_id).select().first()
        assert role.role_kind == KIND_ADMIN
        flag.assert_awaited()
        assert flag.await_args.kwargs["tenant"] == TENANT_SLUG

    async def test_platform_server_id_in_body_is_not_bound(
        self, app: Quart, automation_db: Any, flag: AsyncMock
    ) -> None:
        resp = await app.test_client().post(
            "/api/v1/communities",
            json={"name": "Guild Grab", "platformServerId": "123456789"},
            headers=_hdr(),
        )
        assert resp.status_code == 201
        dal = automation_db.dal
        row = dal(dal.communities.name == "guild-grab").select().first()
        assert row.platform_server_id is None

    async def test_without_scope_is_403_and_creates_nothing(
        self, app: Quart, automation_db: Any, flag: AsyncMock
    ) -> None:
        dal = automation_db.dal
        before = dal(dal.communities).count()
        for scope in ("", "tenant:read community:read"):
            resp = await app.test_client().post(
                "/api/v1/communities", json={"name": "Nope"}, headers=_hdr(scope=scope)
            )
            assert resp.status_code == 403
        assert dal(dal.communities).count() == before
        flag.assert_not_awaited()  # rejected before flag / cap / slot logic

    async def test_same_name_other_tenant_ok_same_tenant_409(
        self, app: Quart, automation_db: Any, flag: AsyncMock
    ) -> None:
        dal = automation_db.dal
        other = dal.tenants.insert(slug="other", display_name="Other", is_active=True)
        dal.communities.insert(name="taken", display_name="t", tenant_id=other, is_active=True)
        dal.commit()
        client = app.test_client()
        ok = await client.post("/api/v1/communities", json={"name": "Taken"}, headers=_hdr())
        assert ok.status_code == 201
        dup = await client.post("/api/v1/communities", json={"name": "taken"}, headers=_hdr())
        assert dup.status_code == 409

    async def test_missing_name_is_400(self, app: Quart, flag: AsyncMock) -> None:
        resp = await app.test_client().post("/api/v1/communities", json={}, headers=_hdr())
        assert resp.status_code == 400


class TestActivity:
    @pytest.fixture
    def seeded(self, app: Quart, automation_db: Any) -> int:
        _users(automation_db)
        cid = seed_community(automation_db)
        seed_membership(automation_db, community_id=cid, user_id=1, role="member")
        dal = automation_db.dal
        now = datetime.now(UTC).replace(tzinfo=None)
        dal(dal.community_members.user_id == "1").update(joined_at=now - timedelta(days=3))
        seed_membership(automation_db, community_id=cid, user_id=2, role="member")
        dal(dal.community_members.user_id == "2").update(joined_at=now - timedelta(days=1))
        seed_membership(automation_db, community_id=cid, user_id=3, role="member")
        dal(dal.community_members.user_id == "3").update(
            joined_at=now, membership_state=2
        )  # banned: excluded
        from services.community_common import ensure_community_tables

        ensure_community_tables(dal, migrate=True)
        dal.announcements.insert(
            community_id=cid,
            title="Hello",
            content="c",
            status="published",
            created_by=1,
            created_at=now - timedelta(days=2),
            published_at=now - timedelta(days=2),
        )
        dal.announcements.insert(
            community_id=cid, title="Draft", content="c", status="draft", created_at=now
        )
        dal.commit()
        return cid

    async def test_ordering_fields_and_exclusions(
        self, app: Quart, automation_db: Any, seeded: int
    ) -> None:
        url = cpath(automation_db, "/api/v1/communities/{community_id}/activity", seeded)
        resp = await app.test_client().get(url, headers=_hdr(user_id=1))
        assert resp.status_code == 200
        items = (await resp.get_json())["items"]
        assert [i["kind"] for i in items] == [1, 2, 1]
        assert all(set(i) == ITEM_KEYS for i in items)
        assert items[1]["title"] == "Hello" and items[0]["title"] is None
        assert [i["at"] for i in items] == sorted((i["at"] for i in items), reverse=True)
        assert all(i["actorId"] is None or len(i["actorId"]) == 36 for i in items)

    async def test_limit_bounds(self, app: Quart, automation_db: Any, seeded: int) -> None:
        url = cpath(automation_db, "/api/v1/communities/{community_id}/activity", seeded)
        client = app.test_client()
        one = await (await client.get(url + "?limit=1", headers=_hdr(user_id=1))).get_json()
        assert len(one["items"]) == 1
        zero = await (await client.get(url + "?limit=0", headers=_hdr(user_id=1))).get_json()
        assert len(zero["items"]) == 1  # clamped up to 1
        big = await (await client.get(url + "?limit=999", headers=_hdr(user_id=1))).get_json()
        assert len(big["items"]) == 3

    async def test_non_member_and_banned_403(
        self, app: Quart, automation_db: Any, seeded: int
    ) -> None:
        url = cpath(automation_db, "/api/v1/communities/{community_id}/activity", seeded)
        client = app.test_client()
        assert (await client.get(url, headers=_hdr(user_id=99))).status_code == 403
        assert (await client.get(url, headers=_hdr(user_id=3))).status_code == 403

    async def test_foreign_tenant_community_404(self, app: Quart, automation_db: Any) -> None:
        dal = automation_db.dal
        other = dal.tenants.insert(slug="other", display_name="Other", is_active=True)
        cid = dal.communities.insert(
            name="foreign", display_name="f", tenant_id=other, is_active=True
        )
        dal.commit()
        seed_membership(SimpleNamespace(dal=dal), community_id=cid, user_id=1, role="member")
        url = cpath(automation_db, "/api/v1/communities/{community_id}/activity", cid)
        resp = await app.test_client().get(url, headers=_hdr(user_id=1))
        assert resp.status_code == 404
