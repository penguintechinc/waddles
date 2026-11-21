"""Route-level proof that `X:read`-only community routes are not open to every signed-in user.

Every session carries the viewer bundle's universal `*:read`, so each token below holds
`*:read` and nothing else. Admin routes must still refuse a plain member, member routes must
refuse a non-member / banned member, foreign-tenant communities are 404, and platform analytics
are refused to a plain session. Never uses `bypass_route_guards`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.analytics as analytics_module
import blueprints.v1.community_chat as chat_module
import blueprints.v1.community_raffle as raffle_module
from blueprints.v1.analytics import analytics_bp
from blueprints.v1.community_chat import chat_bp
from blueprints.v1.community_raffle import raffle_bp
from blueprints.v1.live_activity import live_activity_bp
from tests.community_ids import cpath, install_community_ids
from tests.conftest import (
    OTHER_TENANT_SLUG,
    insert_global_admin,
    make_user_token,
    seed_membership,
)

ADMIN_UID, MEMBER_UID, OUTSIDER_UID, BANNED_UID = 11, 12, 13, 14
VIEWER_SCOPE = "*:read"


@pytest.fixture
def seeded(community_db: Any) -> Any:
    """`(dal, community_id, foreign_community_id)` with an admin, a member and a banned member."""
    dal, community_id = community_db
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=community_id, user_id=ADMIN_UID, role="community-admin")
    seed_membership(db, community_id=community_id, user_id=MEMBER_UID, role="member")
    seed_membership(db, community_id=community_id, user_id=BANNED_UID, role="member")
    # A ban persists as `role='banned'` + `is_active=False` (`ban_suppression`).
    dal(dal.community_members.user_id == str(BANNED_UID)).update(is_active=False)
    other = dal.tenants.insert(slug=OTHER_TENANT_SLUG, is_active=True)
    foreign = dal.communities.insert(name="foreign", tenant_id=other)
    dal.commit()
    return dal, community_id, foreign


@pytest.fixture
def app(seeded: Any, monkeypatch: pytest.MonkeyPatch) -> Quart:
    dal = seeded[0]
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    for bp in (raffle_bp, chat_bp, live_activity_bp, analytics_bp):
        quart_app.register_blueprint(bp)
    monkeypatch.setattr(raffle_module, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_module, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        analytics_module.svc, "platform_overview", AsyncMock(return_value={"success": True})
    )
    return quart_app


def _hdr(uid: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=uid, scope=VIEWER_SCOPE)}"}


def _admin_url(dal: Any, cid: int) -> str:
    return cpath(dal, "/api/v1/admin/{community_id}/raffle-customization", cid)


def _member_url(dal: Any, cid: int) -> str:
    return cpath(dal, "/api/v1/community/{community_id}/chat/channels", cid)


class TestAdminRoute:
    """`GET .../raffle-customization` -- community ADMIN audience."""

    async def test_non_member_is_403(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(
            _admin_url(seeded[0], seeded[1]), headers=_hdr(OUTSIDER_UID)
        )
        assert r.status_code == 403

    async def test_plain_member_is_403(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_admin_url(seeded[0], seeded[1]), headers=_hdr(MEMBER_UID))
        assert r.status_code == 403

    async def test_admin_is_200(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_admin_url(seeded[0], seeded[1]), headers=_hdr(ADMIN_UID))
        assert r.status_code == 200

    async def test_cross_tenant_is_404(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_admin_url(seeded[0], seeded[2]), headers=_hdr(ADMIN_UID))
        assert r.status_code == 404

    async def test_global_admin_token_scope_alone_does_not_cross_tenant(
        self, app: Quart, seeded: Any
    ) -> None:
        token = make_user_token(user_id=OUTSIDER_UID, scope="*:read users:admin")
        r = await app.test_client().get(
            _admin_url(seeded[0], seeded[2]), headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 404


class TestMemberRoute:
    """`GET .../chat/channels` -- community MEMBER audience."""

    async def test_non_member_is_403(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(
            _member_url(seeded[0], seeded[1]), headers=_hdr(OUTSIDER_UID)
        )
        assert r.status_code == 403

    async def test_banned_member_is_403(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_member_url(seeded[0], seeded[1]), headers=_hdr(BANNED_UID))
        assert r.status_code == 403

    async def test_member_is_200(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_member_url(seeded[0], seeded[1]), headers=_hdr(MEMBER_UID))
        assert r.status_code == 200

    async def test_admin_is_200(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_member_url(seeded[0], seeded[1]), headers=_hdr(ADMIN_UID))
        assert r.status_code == 200

    async def test_cross_tenant_is_404(self, app: Quart, seeded: Any) -> None:
        r = await app.test_client().get(_member_url(seeded[0], seeded[2]), headers=_hdr(MEMBER_UID))
        assert r.status_code == 404

    async def test_member_is_403_on_admin_route(self, app: Quart, seeded: Any) -> None:
        url = cpath(seeded[0], "/api/v1/community/{community_id}/live-activity", seeded[1])
        r = await app.test_client().get(url, headers=_hdr(MEMBER_UID))
        assert r.status_code == 403


class TestPlatformAnalytics:
    """`analytics:read` must not be satisfied by `*:read`."""

    async def test_plain_session_is_403(self, app: Quart) -> None:
        r = await app.test_client().get(
            "/api/v1/analytics/platform/overview", headers=_hdr(MEMBER_UID)
        )
        assert r.status_code == 403

    async def test_literal_analytics_scope_is_still_403(self, app: Quart) -> None:
        """A maintainer-bundle token carries `analytics:read` literally -- not enough."""
        token = make_user_token(user_id=ADMIN_UID, scope="*:read analytics:read")
        r = await app.test_client().get(
            "/api/v1/analytics/platform/overview", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 403

    async def test_global_admin_is_200(self, app: Quart, seeded: Any) -> None:
        uid = insert_global_admin(seeded[0])
        r = await app.test_client().get("/api/v1/analytics/platform/overview", headers=_hdr(uid))
        assert r.status_code == 200


class TestEventReadRoutes:
    """event.py ProxyRoutes `rsvp-counts` / `my-permissions` carry a member guard."""

    @pytest.fixture
    def event_app(self, seeded: Any, monkeypatch: pytest.MonkeyPatch) -> Quart:
        import blueprints.v1.event as event_module
        from services.event_calendar_proxy import ProxyResult

        dal = seeded[0]
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.config["dal"] = dal
        install_community_ids(quart_app, dal)
        quart_app.register_blueprint(event_module.event_calendar_admin_bp)
        monkeypatch.setattr(event_module, "feature_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(
            event_module._proxy_client,
            "request",
            AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={"success": True})),
        )
        return quart_app

    @pytest.mark.parametrize("leaf", ["rsvp-counts", "my-permissions"])
    async def test_non_member_403_member_200_cross_tenant_404(
        self, event_app: Quart, seeded: Any, leaf: str
    ) -> None:
        dal, cid, foreign = seeded
        base = "/api/v1/admin/{community_id}/calendar/events/11/" + leaf
        c = event_app.test_client()
        assert (await c.get(cpath(dal, base, cid), headers=_hdr(OUTSIDER_UID))).status_code == 403
        assert (await c.get(cpath(dal, base, cid), headers=_hdr(MEMBER_UID))).status_code == 200
        assert (await c.get(cpath(dal, base, foreign), headers=_hdr(MEMBER_UID))).status_code == 404
