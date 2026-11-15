"""`blueprints/v1/live_status.py` -- gh #287 S10 community-scoped live status routes.

Standalone Quart app registering both `live_status_public_bp` and
`live_status_internal_bp` against this file's own `streaming_db` fixture
(already bound to `coordination`/`community_servers` -- `tests/conftest.
py`), same duplication rationale as `test_v1_stream_blueprint.py`'s own
`app` fixture and `test_v1_community_loyalty.py`'s `SERVICE_API_KEY`
pattern for the internal group.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.live_status import live_status_internal_bp, live_status_public_bp
from config import HubAPIConfig
from tests.conftest import TENANT_SLUG

SERVICE_API_KEY = "test-service-key"


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8205,
        grpc_port=50205,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8205",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(streaming_db: Any, monkeypatch: pytest.MonkeyPatch) -> Quart:
    monkeypatch.setenv("SERVICE_API_KEY", SERVICE_API_KEY)
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(live_status_public_bp)
    quart_app.register_blueprint(live_status_internal_bp)
    quart_app.config["dal"] = streaming_db.dal
    quart_app.config["async_dal"] = streaming_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _tenant_id(db: Any) -> int:
    row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    return int(row.id)


def _seed_community(db: Any, *, is_public: bool = True, is_active: bool = True) -> int:
    community_id: int = db.dal.communities.insert(
        name="acme-community", tenant_id=_tenant_id(db), is_active=is_active, is_public=is_public
    )
    db.dal.commit()
    return community_id


def _seed_live_channel(
    db: Any, *, community_id: int, is_live: bool = True, viewer_count: int = 25
) -> None:
    db.dal.community_servers.insert(
        community_id=community_id, platform="twitch", platform_server_id="srv-1", status="approved"
    )
    db.dal.coordination.insert(
        entity_id="twitch:999",
        platform="twitch",
        server_id="srv-1",
        channel_id="srv-1",
        channel_name="Cool Channel",
        is_live=is_live,
        viewer_count=viewer_count,
        live_since=datetime(2026, 1, 1, tzinfo=UTC) if is_live else None,
    )
    db.dal.commit()


class TestPublicLiveStatus:
    async def test_no_auth_required(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.get(f"/api/v1/public/communities/{community_id}/live")
        assert response.status_code == 200

    async def test_live_community_response_shape(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_live_channel(streaming_db, community_id=community_id)

        response = await client.get(f"/api/v1/public/communities/{community_id}/live")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = await response.get_json()
        data = body["data"]
        assert data["live"] is True
        assert data["platform"] == "twitch"
        assert data["channel"] == "Cool Channel"
        assert data["viewer_count"] == 25
        assert isinstance(data["streams"], list)
        assert data["streams"][0]["live"] is True

    async def test_offline_community_reports_live_false(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_live_channel(streaming_db, community_id=community_id, is_live=False, viewer_count=0)

        response = await client.get(f"/api/v1/public/communities/{community_id}/live")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["live"] is False

    async def test_no_connected_channels_reports_live_false(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.get(f"/api/v1/public/communities/{community_id}/live")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["live"] is False
        assert body["data"]["streams"] == []

    async def test_unknown_community_is_404(self, client: Any, streaming_db: Any) -> None:
        response = await client.get("/api/v1/public/communities/999999/live")
        assert response.status_code == 404

    async def test_private_community_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db, is_public=False)
        response = await client.get(f"/api/v1/public/communities/{community_id}/live")
        assert response.status_code == 404

    async def test_inactive_community_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db, is_active=False)
        response = await client.get(f"/api/v1/public/communities/{community_id}/live")
        assert response.status_code == 404


class TestInternalLiveStatus:
    async def test_missing_service_key_is_401(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.get(f"/api/v1/internal/live?community_id={community_id}")
        assert response.status_code == 401

    async def test_wrong_service_key_is_401(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.get(
            f"/api/v1/internal/live?community_id={community_id}",
            headers={"X-Service-Key": "wrong"},
        )
        assert response.status_code == 401

    async def test_missing_community_id_is_400(self, client: Any, streaming_db: Any) -> None:
        response = await client.get(
            "/api/v1/internal/live", headers={"X-Service-Key": SERVICE_API_KEY}
        )
        assert response.status_code == 400

    async def test_unknown_community_is_404(self, client: Any, streaming_db: Any) -> None:
        response = await client.get(
            "/api/v1/internal/live?community_id=999999", headers={"X-Service-Key": SERVICE_API_KEY}
        )
        assert response.status_code == 404

    async def test_valid_key_returns_live_status_even_for_private_community(
        self, client: Any, streaming_db: Any
    ) -> None:
        """Internal callers bypass the public/is_active/deleted_at gate -- trusted caller."""
        community_id = _seed_community(streaming_db, is_public=False)
        _seed_live_channel(streaming_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/internal/live?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["live"] is True
