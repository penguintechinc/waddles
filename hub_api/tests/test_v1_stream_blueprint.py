"""`blueprints/v1/stream.py` -- characterization tests for the M7 live-stream listing port.

Registers both `community_stream_bp` and `communities_stream_bp` (mirrors
Node's dual `/community` + `/communities` mount, `routes/index.js`) --
one app, one `streaming_db` fixture, real JWTs.

Fail-first proof (executed, not narrated): temporarily changed
`get_live_streams()`'s route decorator from `authorize_community(...,
admin=False)` to skip the call entirely -- `test_non_member_is_403` went
red (200 instead of 403, letting a non-member read another community's
live-stream listing); reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.stream as stream_module
from blueprints.v1.stream import communities_stream_bp, community_stream_bp
from config import HubAPIConfig
from tests.conftest import TENANT_SLUG, make_user_token


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug=DEFAULT_TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(streaming_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(community_stream_bp)
    quart_app.register_blueprint(communities_stream_bp)
    quart_app.config["dal"] = streaming_db.dal
    quart_app.config["async_dal"] = streaming_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the `streaming.stream` two-gate Feature flag ON for every test in this file."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(stream_module, "feature_enabled", stub)
    return stub


def _tenant_id(db: Any) -> int:
    row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    return int(row.id)


def _seed_community(db: Any) -> int:
    community_id: int = db.dal.communities.insert(name="acme-community", tenant_id=_tenant_id(db))
    db.dal.commit()
    return community_id


def _seed_member(db: Any, *, community_id: int, user_id: int) -> None:
    db.dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role="member", is_active=True
    )
    db.dal.commit()


def _seed_live_stream(
    db: Any,
    *,
    community_id: int,
    entity_id: str,
    channel_id: str = "chan-1",
    viewer_count: int = 100,
    is_live: bool = True,
    status: str = "approved",
    platform: str = "twitch",
) -> None:
    db.dal.community_servers.insert(
        community_id=community_id,
        platform=platform,
        platform_server_id="srv-1",
        status=status,
    )
    db.dal.coordination.insert(
        entity_id=entity_id,
        platform=platform,
        server_id="srv-1",
        channel_id=channel_id,
        channel_name="Cool Channel",
        is_live=is_live,
        viewer_count=viewer_count,
        stream_title="Best stream",
        game_name="Just Chatting",
        thumbnail_url="https://cdn.example/thumb.jpg",
        last_updated=datetime.now(UTC),
    )
    db.dal.commit()


class TestAuthAndMembership:
    async def test_missing_token_is_401(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.get(f"/api/v1/communities/{community_id}/streams")
        assert response.status_code == 401

    async def test_non_member_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(f"/api/v1/communities/{community_id}/streams", headers=headers)
        assert response.status_code == 403

    async def test_member_can_read_empty_list(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(f"/api/v1/communities/{community_id}/streams", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["streams"] == []

    async def test_featured_streams_non_member_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            f"/api/v1/communities/{community_id}/streams/featured", headers=headers
        )
        assert response.status_code == 403


class TestBothMountPoints:
    async def test_singular_and_plural_prefix_both_reachable(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1)
        _seed_live_stream(streaming_db, community_id=community_id, entity_id="e1")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}

        singular = await client.get(f"/api/v1/community/{community_id}/streams", headers=headers)
        plural = await client.get(f"/api/v1/communities/{community_id}/streams", headers=headers)
        assert singular.status_code == 200
        assert plural.status_code == 200
        assert (await singular.get_json())["streams"] == (await plural.get_json())["streams"]


class TestLiveStreamListing:
    async def test_only_live_twitch_approved_servers_included(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1)
        _seed_live_stream(
            streaming_db, community_id=community_id, entity_id="live-1", viewer_count=50
        )
        # Not live -- excluded.
        streaming_db.dal.community_servers.insert(
            community_id=community_id,
            platform="twitch",
            platform_server_id="srv-2",
            status="approved",
        )
        streaming_db.dal.coordination.insert(
            entity_id="offline-1",
            platform="twitch",
            server_id="srv-2",
            is_live=False,
            viewer_count=0,
        )
        # Not approved -- excluded.
        streaming_db.dal.community_servers.insert(
            community_id=community_id,
            platform="twitch",
            platform_server_id="srv-3",
            status="pending",
        )
        streaming_db.dal.coordination.insert(
            entity_id="pending-1",
            platform="twitch",
            server_id="srv-3",
            is_live=True,
            viewer_count=10,
        )
        streaming_db.dal.commit()

        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(f"/api/v1/communities/{community_id}/streams", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert [s["entityId"] for s in body["streams"]] == ["live-1"]

    async def test_featured_limits_to_top_5_by_viewer_count(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1)
        for i in range(7):
            streaming_db.dal.community_servers.insert(
                community_id=community_id,
                platform="twitch",
                platform_server_id=f"srv-{i}",
                status="approved",
            )
            streaming_db.dal.coordination.insert(
                entity_id=f"e{i}",
                platform="twitch",
                server_id=f"srv-{i}",
                is_live=True,
                viewer_count=i,
            )
        streaming_db.dal.commit()

        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            f"/api/v1/communities/{community_id}/streams/featured", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["streams"]) == 5
        assert body["streams"][0]["viewerCount"] == 6

    async def test_stream_details_not_found_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            f"/api/v1/communities/{community_id}/streams/does-not-exist", headers=headers
        )
        assert response.status_code == 404

    async def test_stream_details_success_includes_last_activity(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1)
        _seed_live_stream(streaming_db, community_id=community_id, entity_id="e1")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            f"/api/v1/communities/{community_id}/streams/e1", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stream"]["entityId"] == "e1"
        assert body["stream"]["lastActivity"] is not None
