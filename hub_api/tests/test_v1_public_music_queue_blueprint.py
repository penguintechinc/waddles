"""`blueprints/v1/public_music_queue.py` -- unauthenticated public queue-page read.

Standalone Quart app registering only `public_music_queue_bp` against the
`music_station_db` fixture (`tests/conftest.py`) -- no JWT, no
`X-Service-Key`, matching that route's own "pre-auth surface" contract
(see its module docstring). `TestRateLimiting` boots a second app that
ALSO installs `services.rate_limiting.install_rate_limiting()` (the same
global hook `app.py::create_app()` wires over every route in production)
with a deliberately tiny limit, mirroring `tests/test_rate_limiting.py`'s
own fail-first pattern -- proves this route rides the existing app-wide
limiter rather than needing (or accidentally lacking) one of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.public_music_queue import MUSIC_PLAYBACK_REDIS_CONFIG_KEY, public_music_queue_bp
from config import HubAPIConfig
from services.rate_limiting import install_rate_limiting
from tests.conftest import TENANT_SLUG

_ROUTE = "/api/v1/public/communities/{community_id}/music-station/queue"


class FakeRedis:
    """In-memory async Redis/Valkey stand-in -- get/set/delete only, what gh-315 needs.

    See `tests/test_v1_community_music_queue_internal.py::FakeRedis`'s own
    docstring for the full rationale -- duplicated here (not imported)
    since every test file in this port owns its own app fixture.
    """

    def __init__(self) -> None:
        """Start with an empty in-memory key/value store."""
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


#: Exact top-level key set the contract pins -- a regression test fails
#: loudly the moment an extra (potentially PII-bearing) field sneaks in.
_EXPECTED_ITEM_KEYS = {
    "id",
    "position",
    "status",
    "title",
    "artist",
    "duration_ms",
    "artwork_url",
    "provider",
    "external_id",
    "url",
    "eta_seconds",
    "started_at",
    "requested_by",
}
_EXPECTED_REQUESTED_BY_KEYS = {"display_name", "platform"}


def _test_config(**overrides: Any) -> HubAPIConfig:
    base: dict[str, Any] = {
        "module_name": "hub-api-test",
        "module_version": "0.0.0-test",
        "module_port": 8206,
        "grpc_port": 50206,
        "database_url": "sqlite:memory",
        "database_read_replica_url": None,
        "db_pool_size": 1,
        "db_max_retries": 1,
        "db_retry_delay": 1,
        "secret_key": "change-me-in-production",
        "jwt_algorithm": "HS256",
        "default_tenant_slug": "default",
        "posthog_api_key": None,
        "posthog_host": "https://license.penguintech.io",
        "license_server_url": "https://license.penguintech.io",
        "identity_callback_base_url": "http://localhost:8206",
        "frontend_origin": "http://localhost:5173",
        "log_level": "INFO",
    }
    base.update(overrides)
    return HubAPIConfig(**base)


@pytest.fixture
def app(music_station_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(public_music_queue_bp)
    quart_app.config["dal"] = music_station_db.dal
    quart_app.config["async_dal"] = music_station_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    quart_app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY] = FakeRedis()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_community(
    db: Any, *, is_public: bool = True, is_active: bool = True, deleted_at: Any = None
) -> int:
    tenant_row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    community_id = db.dal.communities.insert(
        name="test-community",
        display_name="Test Community",
        tenant_id=tenant_row.id,
        is_public=is_public,
        is_active=is_active,
        deleted_at=deleted_at,
    )
    db.dal.commit()
    return int(community_id)


def _seed_track(db: Any, *, duration_ms: int = 210000) -> int:
    track_id = db.dal.music_tracks.insert(
        tenant_id=1,
        provider="youtube",
        external_id="abc123",
        title="Track Title",
        artist="Track Artist",
        duration_ms=duration_ms,
        artwork_url=None,
        url="https://youtube.com/watch?v=abc123",
        created_at=datetime.now(UTC),
    )
    db.dal.commit()
    return int(track_id)


def _seed_queue_item(
    db: Any,
    *,
    community_id: int,
    track_id: int,
    status: str,
    position: int = 1,
    started_at: datetime | None = None,
    requested_by: int | None = None,
) -> int:
    queue_id = db.dal.music_station_queue.insert(
        tenant_id=1,
        community_id=community_id,
        track_id=track_id,
        position=position,
        status=status,
        source="request",
        playlist_id=None,
        requested_by=requested_by,
        added_at=datetime.now(UTC),
        started_at=started_at,
    )
    db.dal.commit()
    return int(queue_id)


class TestVisibility:
    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.get(_ROUTE.format(community_id=999999))
        assert response.status_code == 404

    async def test_private_community_is_404(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db, is_public=False)
        response = await client.get(_ROUTE.format(community_id=community_id))
        assert response.status_code == 404

    async def test_inactive_community_is_404(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db, is_active=False)
        response = await client.get(_ROUTE.format(community_id=community_id))
        assert response.status_code == 404

    async def test_deleted_community_is_404(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db, deleted_at=datetime.now(UTC))
        response = await client.get(_ROUTE.format(community_id=community_id))
        assert response.status_code == 404

    async def test_public_active_community_is_200(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.get(_ROUTE.format(community_id=community_id))
        assert response.status_code == 200


class TestShapeAndNoPII:
    """Exact key-set assertions -- an over-exposed field must fail loudly, not silently ship."""

    async def test_empty_queue_shape(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.get(_ROUTE.format(community_id=community_id))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert set(body["data"].keys()) == {"community", "now_playing", "queue", "playback"}
        assert body["data"]["community"] == {"id": community_id, "name": "Test Community"}
        assert body["data"]["now_playing"] is None
        assert body["data"]["queue"] == []
        assert response.headers.get("Cache-Control") == "no-store"
        # Nothing playing -> no position to report, and never paused by default.
        assert body["data"]["playback"] == {
            "paused": False,
            "paused_since": None,
            "position_ms": None,
        }

    async def test_playback_shape_has_no_pii(self, client: Any, music_station_db: Any) -> None:
        """`data.playback` (gh-315) is exactly 3 fields, none of them PII."""
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=600000)
        _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC),
        )

        response = await client.get(_ROUTE.format(community_id=community_id))
        body = await response.get_json()
        playback = body["data"]["playback"]
        assert set(playback.keys()) == {"paused", "paused_since", "position_ms"}
        assert playback["paused"] is False
        assert playback["paused_since"] is None
        assert isinstance(playback["position_ms"], int)

    async def test_queue_item_key_set_has_no_pii(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        music_station_db.dal.community_members.insert(
            community_id=community_id,
            user_id="7",
            platform="twitch",
            platform_user_id="abc123",
            display_name="PenguinFan42",
            is_active=True,
        )
        music_station_db.dal.commit()
        track_id = _seed_track(music_station_db)
        _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC),
            requested_by=7,
        )

        response = await client.get(_ROUTE.format(community_id=community_id))
        body = await response.get_json()
        item = body["data"]["now_playing"]
        assert set(item.keys()) == _EXPECTED_ITEM_KEYS
        assert set(item["requested_by"].keys()) == _EXPECTED_REQUESTED_BY_KEYS
        assert item["requested_by"] == {"display_name": "PenguinFan42", "platform": "twitch"}
        # No raw hub_users id, email, or platform_user_id anywhere in the payload.
        assert "requested_by_id" not in item
        assert "user_id" not in item
        assert "email" not in item
        assert "platform_user_id" not in item["requested_by"]


class TestNoAutoAdvanceSideEffect:
    """The public route is a pure read -- it must never itself mutate queue state."""

    async def test_expired_playing_item_is_not_advanced(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=1000)
        expired_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC) - timedelta(seconds=30),
        )

        response = await client.get(_ROUTE.format(community_id=community_id))
        assert response.status_code == 200
        body = await response.get_json()
        # Still reported as playing -- the (expired) state as it stood, not advanced.
        assert body["data"]["now_playing"]["id"] == expired_id
        assert body["data"]["now_playing"]["status"] == "playing"

        row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == expired_id)
            .select()
            .first()
        )
        assert row.status == "playing"  # unchanged in the DB, not just the response


class TestRateLimiting:
    """Fail-first proof: this route rides `app.py`'s existing global rate limiter."""

    @pytest.fixture
    def limited_app(self, music_station_db: Any) -> Quart:
        cfg = _test_config(
            rate_limit_max_requests=3,
            rate_limit_window_seconds=60,
            rate_limit_auth_max_requests=1000,
            rate_limit_auth_window_seconds=60,
            # No REDIS_URL reachable in the test sandbox -- `RateLimiter.
            # connect()` fails open to its in-memory fallback (see
            # `tests/test_rate_limiting.py`'s own identical setup), but
            # ONLY once `connect()` has actually run -- see this class's
            # `test_returns_429_after_limit_exceeded` own `test_app()`
            # wrapper below, which triggers `app.py::create_app()`'s
            # matching `before_serving` hook.
            valkey_url="redis://unreachable-in-tests.invalid:6379/0",
        )
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        rate_limiter = install_rate_limiting(quart_app, cfg)
        quart_app.register_blueprint(public_music_queue_bp)
        quart_app.config["dal"] = music_station_db.dal
        quart_app.config["async_dal"] = music_station_db
        quart_app.config["HUB_API_CONFIG"] = cfg

        @quart_app.before_serving
        async def _connect_rate_limiter() -> None:
            await rate_limiter.connect()

        return quart_app

    async def test_returns_429_after_limit_exceeded(
        self, limited_app: Quart, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        async with limited_app.test_app():
            client = limited_app.test_client()
            statuses = []
            for _ in range(4):
                response = await client.get(_ROUTE.format(community_id=community_id))
                statuses.append(response.status_code)

        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429
