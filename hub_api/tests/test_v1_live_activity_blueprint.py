"""`blueprints/v1/live_activity.py` -- list route + `/stream` route auth/tenant-scoping.

Reuses `community_db`/`auth_headers` from `tests/conftest.py` unchanged
(no new fixture needed). The SSE generator's own poll/emit/scoping
behavior is unit-tested directly in `tests/test_live_activity_service.py`
against `services.live_activity.event_stream` -- an infinite generator
can't safely be drained through `client.get()` (it would hang the test),
so this file only exercises `/stream`'s auth-gate and tenant-scoping
short-circuits (both return before the streaming `Response` is ever
constructed) plus the happy-path response shape (status/mimetype only,
never reading the body).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.live_activity import live_activity_bp
from services import live_activity as svc
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    # `_ensure_table(migrate=False)` (the default, matching production --
    # schema owned elsewhere) is a no-op DDL-wise; this `sqlite:memory`
    # fixture has no real migration behind it, so the table must be
    # created here, once, with `migrate=True` -- mirrors every other
    # `bind_<group>_tables(dal, migrate=True)` call `tests/conftest.py`'s
    # own fixtures make for the exact same reason.
    svc._ensure_table(dal, migrate=True)
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(live_activity_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_event(dal: Any, *, community_id: int, **overrides: Any) -> int:
    svc._ensure_table(dal, migrate=True)
    fields: dict[str, Any] = {
        "community_id": community_id,
        "platform": "twitch",
        "actor": "alice",
        "message_in": "!hello",
        "reply_out": "hi!",
        "channel_id": "chan-1",
        "occurred_at": datetime.now(UTC),
    }
    fields.update(overrides)
    event_id: int = dal.live_activity_events.insert(**fields)
    dal.commit()
    return event_id


class TestListLiveActivity:
    async def test_no_auth_header_is_401(self, client: Any, community_db: Any) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity"
        )
        assert response.status_code == 401

    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity",
            headers=auth_headers(scope="community.live_activity:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/community/{UNKNOWN_PUBLIC_ID}/live-activity",
            headers=auth_headers(scope="community.live_activity:read"),
        )
        assert response.status_code == 404

    async def test_empty_list_shape(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity",
            headers=auth_headers(scope="community.live_activity:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "events": []}

    async def test_lists_newest_first_and_respects_limit(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        _seed_event(dal, community_id=community_id, actor="alice")
        second_id = _seed_event(dal, community_id=community_id, actor="bob")

        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity?limit=1",
            headers=auth_headers(scope="community.live_activity:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["events"]) == 1
        assert body["events"][0]["id"] == second_id
        assert body["events"][0]["actor"] == "bob"

    async def test_community_scoping_excludes_other_communities(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        own_tenant_id = dal(dal.communities.id == community_id).select().first().tenant_id
        other_id = dal.communities.insert(name="other-community", tenant_id=own_tenant_id)
        dal.commit()
        _seed_event(dal, community_id=other_id, actor="eve")

        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity",
            headers=auth_headers(scope="community.live_activity:read"),
        )
        body = await response.get_json()
        assert body["events"] == []


class TestStreamLiveActivity:
    async def test_no_auth_header_is_401(self, client: Any, community_db: Any) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity/stream"
        )
        assert response.status_code == 401

    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/live-activity/stream",
            headers=auth_headers(scope="community.live_activity:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/community/{UNKNOWN_PUBLIC_ID}/live-activity/stream",
            headers=auth_headers(scope="community.live_activity:read"),
        )
        assert response.status_code == 404

    async def test_authorized_request_opens_event_stream(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """Streaming test-client pattern.

        Plain `client.get()` on an infinite generator body would hang the
        test; mirrors `core/svc_presentation/tests/test_push_endpoint.py::
        test_push_fans_out_to_a_live_sse_connection`'s own precedent for
        this exact shape.
        """
        _, community_id = community_db
        connection = client.request(
            path=f"/api/v1/community/{public_id(community_db[0], community_id)}"
            f"/live-activity/stream",
            headers=auth_headers(scope="community.live_activity:read"),
        )
        async with connection as live:
            # `status_code`/`headers` are only populated once the ASGI
            # `http.response.start` message has been sent, which always
            # precedes the first body chunk -- assert after the first
            # `receive()`, not before (a race against the app task
            # otherwise: `__aenter__` only schedules it, it may not have
            # run yet).
            first_chunk = await asyncio.wait_for(live.receive(), timeout=3)
            assert first_chunk == b": keepalive\n\n"
            assert live.status_code == 200
            assert live.headers["Content-Type"].startswith("text/event-stream")
            assert live.headers["Cache-Control"] == "no-cache"
            await live.disconnect()


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
