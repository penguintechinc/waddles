"""Streaming blueprints -- two-gate Feature guard proofs (SCCEBM per-capability flags).

Dedicated fail-first-verify coverage for the one-line `feature_enabled(...)`
guards added this PR to `stream.py` (`streaming.stream`, free),
`streaming.py` (`streaming.broadcast`, the module's one Professional-tier
capability), `calls.py` (`streaming.rtc`, free), `music.py`
(`streaming.music_station`, free), and `overlay.py` (`streaming.overlays`,
free). Every OTHER test file for these blueprints
(`test_v1_stream_blueprint.py` et al.) defaults the gate ON via its own
`_feature_enabled_default_on` autouse fixture and exercises
routing/scope/tenant/proxy behavior instead -- this file is the
complementary "the gate itself actually gates" proof.

Fail-first proof (executed, not narrated): temporarily reverted
`streaming.py::get_stream_config`'s guard (removed the `if not await
feature_enabled(...)` block) and re-ran
`test_broadcast_config_blocked_below_professional_tier` -- it went red
(`assert 200 == 402`). Reverted; green again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.calls as calls_module
import blueprints.v1.music as music_module
import blueprints.v1.overlay as overlay_module
import blueprints.v1.stream as stream_module
import blueprints.v1.streaming as streaming_module
from blueprints.v1.calls import calls_admin_bp
from blueprints.v1.music import music_bp
from blueprints.v1.overlay import overlay_bp
from blueprints.v1.stream import community_stream_bp
from blueprints.v1.streaming import streaming_bp
from config import HubAPIConfig


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


class TestStreamFeatureGate:
    @pytest.fixture
    def app(self, overlay_db: Any) -> Quart:
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(community_stream_bp)
        quart_app.config["dal"] = overlay_db.dal
        quart_app.config["async_dal"] = overlay_db
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_live_streams_blocked_when_flag_off(
        self, client: Any, overlay_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.conftest import TENANT_SLUG, make_user_token

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(stream_module, "feature_enabled", gate)
        monkeypatch.setattr(
            stream_module,
            "authorize_community",
            AsyncMock(return_value=None),
        )
        token = make_user_token(user_id=1, tenant=TENANT_SLUG, scope="*:read")

        response = await client.get(
            "/api/v1/community/1/streams", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.streaming.stream"


class TestBroadcastFeatureGateIsProfessionalTier:
    """`streaming.broadcast` is the Streaming module's one Professional-tier capability."""

    @pytest.fixture
    def app(self, overlay_db: Any) -> Quart:
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(streaming_bp)
        quart_app.config["dal"] = overlay_db.dal
        quart_app.config["async_dal"] = overlay_db
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_broadcast_config_blocked_below_professional_tier(
        self, client: Any, overlay_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.conftest import TENANT_SLUG, make_user_token

        gate = AsyncMock(return_value=False)  # free tier does not entitle streaming.broadcast
        monkeypatch.setattr(streaming_module, "feature_enabled", gate)
        monkeypatch.setattr(
            streaming_module,
            "authorize_community",
            AsyncMock(return_value=None),
        )
        token = make_user_token(user_id=1, tenant=TENANT_SLUG, scope="*:read")

        response = await client.get(
            "/api/v1/admin/1/streams", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.streaming.broadcast"


class TestRtcFeatureGate:
    @pytest.fixture
    def app(self, overlay_db: Any) -> Quart:
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(calls_admin_bp)
        quart_app.config["dal"] = overlay_db.dal
        quart_app.config["async_dal"] = overlay_db
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_call_rooms_blocked_when_flag_off(
        self, client: Any, overlay_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.conftest import TENANT_SLUG, make_user_token

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(calls_module, "feature_enabled", gate)
        monkeypatch.setattr(calls_module, "_require_admin", AsyncMock(return_value=None))
        token = make_user_token(user_id=1, tenant=TENANT_SLUG, scope="streaming.calls:admin")

        response = await client.get(
            "/api/v1/admin/1/calls/rooms", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.streaming.rtc"


class TestMusicStationFeatureGate:
    @pytest.fixture
    def app(self, streaming_db: Any) -> Quart:
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(music_bp)
        quart_app.config["dal"] = streaming_db.dal
        quart_app.config["async_dal"] = streaming_db
        quart_app.config["HUB_API_CONFIG"] = _test_config()
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_music_settings_blocked_when_flag_off(
        self, client: Any, streaming_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.conftest import TENANT_SLUG, make_user_token

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(music_module, "feature_enabled", gate)
        monkeypatch.setattr(music_module, "authorize_community", AsyncMock(return_value=None))
        token = make_user_token(user_id=1, tenant=TENANT_SLUG, scope="*:read")

        response = await client.get(
            "/api/v1/admin/1/music/settings", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.streaming.music_station"


class TestOverlaysFeatureGate:
    @pytest.fixture
    def app(self, overlay_db: Any) -> Quart:
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(overlay_bp)
        quart_app.config["dal"] = overlay_db.dal
        quart_app.config["async_dal"] = overlay_db
        quart_app.config["HUB_API_CONFIG"] = _test_config()
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_get_overlay_blocked_when_flag_off(
        self, client: Any, overlay_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.conftest import TENANT_SLUG, make_user_token

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(overlay_module, "feature_enabled", gate)
        monkeypatch.setattr(
            overlay_module.community_access,
            "require_community_admin",
            AsyncMock(return_value=None),
        )
        token = make_user_token(user_id=1, tenant=TENANT_SLUG, scope="streaming.overlay:admin")

        response = await client.get(
            "/api/v1/admin/1/overlay", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.streaming.overlays"
