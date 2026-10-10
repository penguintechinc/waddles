"""`blueprints/v1/community_music_queue.py`'s internal (service-key) enqueue route.

Standalone Quart app registering `music_internal_bp` against the
`music_station_db` fixture (`tests/conftest.py`) -- the chat-command path
(`core/svc_action/builtin_handlers/social_music_action.py`, `!sr`/`!songrequest`)
has no user JWT, so it calls this route with `X-Service-Key` auth instead
of the admin-scoped `music_queue_bp` route
(`tests/test_v1_community_music_queue_blueprint.py` covers that one, and
owns `fake_resolve`'s own real-`resolve()`-vs-mocked rationale, not
re-derived here).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_music_queue as community_music_queue_module
from blueprints.v1.community_music_queue import (
    MUSIC_PLAYBACK_REDIS_CONFIG_KEY,
    music_internal_bp,
    music_queue_bp,
)
from config import HubAPIConfig
from services import music_status_service
from services.music_providers.track import Track
from tests.conftest import TENANT_SLUG

SERVICE_API_KEY = "test-service-key"
_ROUTE = "/api/v1/internal/music/queue/requests"
_STATUS_ROUTE = "/api/v1/internal/music/status"
_LIVE_QUEUE_ROUTE = "/api/v1/internal/music/queue"
_LIVE_ADVANCE_ROUTE = "/api/v1/internal/music/queue/advance"
_POLICY_ROUTE = "/api/v1/internal/music/policy"
_PLAYBACK_ROUTE = "/api/v1/internal/music/playback"


class FakeRedis:
    """In-memory async Redis/Valkey stand-in -- get/set/delete only, what gh-315 needs.

    No test app in this file calls `services.rate_limiting.
    install_rate_limiting()`, so `blueprints/v1/community_music_queue.py::
    _redis_client()`'s "reuse `RateLimiter._redis`" branch never fires --
    injecting this directly onto `app.config[MUSIC_PLAYBACK_REDIS_CONFIG_
    KEY]` short-circuits that helper's own lazy-real-client fallback
    before it ever tries to open a real connection.
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
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8205",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture(autouse=True)
def _service_key_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("SERVICE_API_KEY", SERVICE_API_KEY)


@pytest.fixture
def app(music_station_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(music_internal_bp)
    quart_app.register_blueprint(music_queue_bp)
    quart_app.config["dal"] = music_station_db.dal
    quart_app.config["async_dal"] = music_station_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    quart_app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY] = FakeRedis()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def fake_resolve(monkeypatch: Any) -> None:
    """Deterministic stand-in for the real, network-calling `resolve()`."""
    _set_fake_resolve(monkeypatch)


def _set_fake_resolve(
    monkeypatch: Any, *, track_provider: str = "youtube", labels: tuple[str, ...] = ()
) -> None:
    """Same fake `resolve()` as `fake_resolve`, with configurable provider/labels (gh-313)."""

    async def _fake(
        url_or_query: str,
        provider: str | None = None,
        *,
        db: Any = None,
        community_id: int | None = None,
    ) -> Track:
        return Track(
            provider=provider or track_provider,
            external_id=url_or_query,
            title=f"Track for {url_or_query}",
            artist="Test Artist",
            duration_ms=210000,
            artwork_url=None,
            url=url_or_query,
            labels=labels,
        )

    monkeypatch.setattr("services.community_music_queue_service.resolve", _fake)


@pytest.fixture(autouse=True)
def _youtube_labels_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the `waddles.social.music.youtube_labels` flag ON for this file's tests (gh-313)."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(community_music_queue_module, "feature_enabled", stub)
    return stub


def _seed_community(db: Any) -> int:
    tenant_row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    community_id = db.dal.communities.insert(name="test-community", tenant_id=tenant_row.id)
    db.dal.commit()
    return int(community_id)


def _seed_track(db: Any, *, tenant_id: int = 1, duration_ms: int = 210000) -> int:
    track_id = db.dal.music_tracks.insert(
        tenant_id=tenant_id,
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


class TestServiceKeyAuth:
    """Fail-first gate: no valid `X-Service-Key` -> 401, never a bare-JWT bypass."""

    async def test_missing_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _ROUTE, json={"communityId": community_id, "urlOrQuery": "some song"}
        )
        assert response.status_code == 401

    async def test_wrong_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": "wrong-key"},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 401


class TestEnqueue:
    """Service-key-authenticated enqueue -- payload validation, tenant derivation, resolution."""

    async def test_enqueue_with_service_key_succeeds(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={
                "communityId": community_id,
                "urlOrQuery": "never gonna give you up",
                "platform": "twitch",
                "platformUserId": "abc123",
                "requestedByDisplay": "penguin",
            },
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["success"] is True
        assert body["item"]["track"]["title"] == "Track for never gonna give you up"
        assert body["item"]["communityId"] == community_id
        assert body["item"]["requestedBy"] is None  # unlinked platform identity
        # Sole item in an empty queue: nothing ahead, nothing playing -> "next up" (0).
        assert body["item"]["etaSeconds"] == 0

    async def test_enqueue_resolves_hub_user_id_from_linked_platform_identity(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        """A viewer whose platform identity is linked in `community_members` gets attribution."""
        community_id = _seed_community(music_station_db)
        music_station_db.dal.community_members.insert(
            community_id=community_id,
            user_id="7",
            platform="twitch",
            platform_user_id="abc123",
        )
        music_station_db.dal.commit()

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={
                "communityId": community_id,
                "urlOrQuery": "some song",
                "platform": "twitch",
                "platformUserId": "abc123",
            },
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["item"]["requestedBy"] == 7

    async def test_enqueue_eta_seconds_sums_duration_of_items_ahead(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        """A second request's ETA = the first (still-queued) item's `durationMs` in seconds."""
        community_id = _seed_community(music_station_db)
        first = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "song one"},
        )
        assert (await first.get_json())["item"]["etaSeconds"] == 0

        second = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "song two"},
        )
        assert second.status_code == 201
        body = await second.get_json()
        # fake_resolve() always returns duration_ms=210000 -> 210 seconds ahead.
        assert body["item"]["etaSeconds"] == 210
        assert body["item"]["position"] == 2

    async def test_missing_community_id_is_400(self, client: Any) -> None:
        response = await client.post(
            _ROUTE, headers={"X-Service-Key": SERVICE_API_KEY}, json={"urlOrQuery": "some song"}
        )
        assert response.status_code == 400

    async def test_blank_url_or_query_is_400(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "   "},
        )
        assert response.status_code == 400

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": 999999, "urlOrQuery": "some song"},
        )
        assert response.status_code == 404

    async def test_no_track_found_is_422(self, client: Any, music_station_db: Any) -> None:
        """An unmocked `resolve()` call with an unsupported provider -> deterministic 422."""
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={
                "communityId": community_id,
                "urlOrQuery": "some song",
                "provider": "not-a-real-provider",
            },
        )
        assert response.status_code == 422

    async def test_enqueue_survives_communities_schema_drift(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        """Regression: `communities.about_extended` (and 4 siblings) are bound in prod.

        `services/schema.py` for pydal query-building but were never added by
        any numbered migration against real Postgres -- a documented,
        pre-existing gap (see that module's docstring, gap 4). This fixture's
        `bind_auth_tables(dal, migrate=True)` auto-creates every bound column
        (including the gap ones), masking the drift -- so it's dropped here to
        reproduce prod's real schema. Before the fix, `internal_enqueue_song_
        request` ran a bare `dal.communities....select()`, which pulls every
        bound field and 500s with `psycopg2.errors.UndefinedColumn` the moment
        a gap column is missing; the fix restricts the select to the two
        columns this handler actually needs.
        """
        community_id = _seed_community(music_station_db)
        music_station_db.dal.executesql("ALTER TABLE communities DROP COLUMN about_extended")
        music_station_db.dal.commit()

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["success"] is True

    async def test_enqueue_unhandled_error_returns_json_body(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        """Regression: an unhandled exception must never surface as an empty-body 500.

        `social_music_action._enqueue()`
        (`core/svc_action`) parses `error.message` from the JSON body to
        decide its chat reply and to log the real cause; an empty body logs
        `message=` with no diagnostic value (the exact symptom this fixes).
        Simulates any still-unhandled DB error (not just the schema-drift
        case above, which is now avoided) by dropping a column the handler
        does NOT defend against.
        """
        community_id = _seed_community(music_station_db)
        music_station_db.dal.executesql("ALTER TABLE communities DROP COLUMN tenant_id")
        music_station_db.dal.commit()

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 500
        body = await response.get_json()
        assert body is not None
        assert body["success"] is False
        assert body["error"]["message"]  # non-empty -- the bug being fixed


class TestInternalSetPolicy:
    """`PUT /api/v1/internal/music/policy` -- service-key gated `youtube_allowed_labels` writes.

    gh-313: lets a chat-side moderation command update the allowlist
    without a user JWT -- same validator (`services.
    community_music_queue_service._normalize_youtube_allowed_labels`) as
    the admin-JWT `music_queue_bp.set_policy` route.
    """

    async def test_missing_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.put(
            _POLICY_ROUTE,
            json={"community_id": community_id, "youtube_allowed_labels": ["music"]},
        )
        assert response.status_code == 401

    async def test_wrong_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": "wrong-key"},
            json={"community_id": community_id, "youtube_allowed_labels": ["music"]},
        )
        assert response.status_code == 401

    async def test_missing_fields_is_400(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id},
        )
        assert response.status_code == 400

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": 999999, "youtube_allowed_labels": ["music"]},
        )
        assert response.status_code == 404

    async def test_validation_failure_is_400_with_exact_message(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "youtube_allowed_labels": ["x" * 65]},
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert body["error"]["message"] == "youtube-labels: up to 32 labels, 64 chars each"

    async def test_set_then_clear_round_trip(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        set_response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "youtube_allowed_labels": [" Music ", "MUSIC"]},
        )
        assert set_response.status_code == 200
        set_body = await set_response.get_json()
        assert set_body["status"] == "success"
        assert set_body["data"]["community_id"] == community_id
        assert set_body["data"]["youtube_allowed_labels"] == ["music"]

        clear_response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "youtube_allowed_labels": []},
        )
        assert clear_response.status_code == 200
        clear_body = await clear_response.get_json()
        assert clear_body["data"]["youtube_allowed_labels"] == []


class TestYoutubeLabelGateInternal:
    """gh-313 label-allowlist gate exercised through the service-key `!sr` enqueue route."""

    async def _set_allowed_labels(self, client: Any, community_id: int, labels: list[str]) -> None:
        response = await client.put(
            _POLICY_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "youtube_allowed_labels": labels},
        )
        assert response.status_code == 200

    async def test_empty_allowlist_accepts(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, labels=("comedy",))
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 201

    async def test_label_match_accepts(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, labels=("gaming", "music"))
        community_id = _seed_community(music_station_db)
        await self._set_allowed_labels(client, community_id, ["music"])

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 201

    async def test_no_match_rejected_with_code_and_message(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, labels=("comedy",))
        community_id = _seed_community(music_station_db)
        await self._set_allowed_labels(client, community_id, ["a", "b", "c"])

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 422
        body = await response.get_json()
        assert body["error"]["code"] == "youtube_label_not_allowed"
        assert body["error"]["message"] == "that video isn't allowed here (allowed: a, b, c)"

    async def test_spotify_track_bypasses_gate(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, track_provider="spotify", labels=())
        community_id = _seed_community(music_station_db)
        await self._set_allowed_labels(client, community_id, ["music"])

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song", "provider": "spotify"},
        )
        assert response.status_code == 201

    async def test_flag_off_bypasses_gate(
        self,
        client: Any,
        music_station_db: Any,
        monkeypatch: Any,
        _youtube_labels_flag_default_on: AsyncMock,
    ) -> None:
        _youtube_labels_flag_default_on.return_value = False
        _set_fake_resolve(monkeypatch, labels=("comedy",))
        community_id = _seed_community(music_station_db)
        await self._set_allowed_labels(client, community_id, ["music"])

        response = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "some song"},
        )
        assert response.status_code == 201


class TestMusicStatus:
    """`GET /api/v1/internal/music/status` -- backs `!sr status`'s enabled/error replies."""

    @pytest.fixture(autouse=True)
    def _reset_health_cache(self) -> Any:
        """`check_spotify_health()`'s module-level cache must not leak between tests."""
        music_status_service._health_cache = None
        yield
        music_status_service._health_cache = None

    async def test_missing_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.get(f"{_STATUS_ROUTE}?community_id={community_id}")
        assert response.status_code == 401

    async def test_missing_community_id_is_400(self, client: Any) -> None:
        response = await client.get(_STATUS_ROUTE, headers={"X-Service-Key": SERVICE_API_KEY})
        assert response.status_code == 400

    async def test_non_integer_community_id_is_400(self, client: Any) -> None:
        response = await client.get(
            f"{_STATUS_ROUTE}?community_id=not-a-number",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 400

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.get(
            f"{_STATUS_ROUTE}?community_id=999999", headers={"X-Service-Key": SERVICE_API_KEY}
        )
        assert response.status_code == 404

    async def test_healthy_provider_returns_enabled_state(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        community_id = _seed_community(music_station_db)

        async def _healthy() -> music_status_service.ProviderHealth:
            return music_status_service.ProviderHealth(healthy=True, cause=None)

        monkeypatch.setattr("blueprints.v1.community_music_queue.check_spotify_health", _healthy)

        response = await client.get(
            f"{_STATUS_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert body["data"]["state"] == "enabled"
        assert body["data"]["cause"] is None
        assert body["data"]["provider"] == "spotify"
        assert body["data"]["queue_length"] == 0

    async def test_unhealthy_provider_returns_error_state_with_cause(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        community_id = _seed_community(music_station_db)

        async def _unhealthy() -> music_status_service.ProviderHealth:
            return music_status_service.ProviderHealth(
                healthy=False, cause="spotify oauth token didn't work (401)"
            )

        monkeypatch.setattr("blueprints.v1.community_music_queue.check_spotify_health", _unhealthy)

        response = await client.get(
            f"{_STATUS_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["state"] == "error"
        assert body["data"]["cause"] == "spotify oauth token didn't work (401)"

    async def test_queue_length_counts_queued_and_playing_but_not_removed(
        self, client: Any, music_station_db: Any, fake_resolve: None, monkeypatch: Any
    ) -> None:
        community_id = _seed_community(music_station_db)

        async def _healthy() -> music_status_service.ProviderHealth:
            return music_status_service.ProviderHealth(healthy=True, cause=None)

        monkeypatch.setattr("blueprints.v1.community_music_queue.check_spotify_health", _healthy)

        await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "song one"},
        )
        second = await client.post(
            _ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"communityId": community_id, "urlOrQuery": "song two"},
        )
        second_queue_id = (await second.get_json())["item"]["id"]
        # Directly flip one item to `removed` (bypassing the admin-scoped
        # moderation route, which needs a JWT this test module doesn't set
        # up) -- isolates queue_length's own status-filtering logic.
        music_station_db.dal(music_station_db.dal.music_station_queue.id == second_queue_id).update(
            status="removed"
        )
        music_station_db.dal.commit()

        response = await client.get(
            f"{_STATUS_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        body = await response.get_json()
        assert body["data"]["queue_length"] == 1


class TestLiveQueueRead:
    """`GET /api/v1/internal/music/queue` -- lazy auto-advance-on-read for the overlay/page."""

    async def test_missing_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.get(f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}")
        assert response.status_code == 401

    async def test_missing_community_id_is_400(self, client: Any) -> None:
        response = await client.get(_LIVE_QUEUE_ROUTE, headers={"X-Service-Key": SERVICE_API_KEY})
        assert response.status_code == 400

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id=999999",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 404

    async def test_empty_queue_returns_null_now_playing(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert body["data"]["community_id"] == community_id
        assert body["data"]["now_playing"] is None
        assert body["data"]["queue"] == []
        assert "updated_at" in body["data"]

    async def test_auto_starts_head_item_when_nothing_playing(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db)
        queue_id = _seed_queue_item(
            music_station_db, community_id=community_id, track_id=track_id, status="queued"
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["now_playing"]["id"] == queue_id
        assert body["data"]["now_playing"]["status"] == "playing"
        assert body["data"]["now_playing"]["started_at"] is not None
        assert body["data"]["queue"] == []

        # Persisted, not just reflected in the response.
        row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == queue_id)
            .select()
            .first()
        )
        assert row.status == "playing"

    async def test_expired_playing_item_auto_advances(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=1000)  # 1s track
        expired_started_at = datetime.now(UTC) - timedelta(seconds=30)  # well past 1s + grace
        expired_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=expired_started_at,
        )
        next_track_id = _seed_track(music_station_db)
        next_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=next_track_id,
            status="queued",
            position=1,
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["now_playing"]["id"] == next_id
        assert body["data"]["queue"] == []

        expired_row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == expired_id)
            .select()
            .first()
        )
        assert expired_row.status == "played"
        assert expired_row.ended_at is not None

    async def test_not_yet_expired_playing_item_does_not_advance(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=600000)  # 10 minute track
        playing_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC),
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["now_playing"]["id"] == playing_id
        assert body["data"]["now_playing"]["status"] == "playing"

    async def test_concurrent_double_read_only_advances_once(
        self, client: Any, music_station_db: Any
    ) -> None:
        """Two overlay instances polling at once never double-advance the same expiry."""
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=1000)
        expired_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        next_track_id = _seed_track(music_station_db)
        next_id = _seed_queue_item(
            music_station_db, community_id=community_id, track_id=next_track_id, status="queued"
        )

        url = f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}"
        headers = {"X-Service-Key": SERVICE_API_KEY}
        first, second = await asyncio.gather(
            client.get(url, headers=headers), client.get(url, headers=headers)
        )
        assert first.status_code == 200
        assert second.status_code == 200

        # Exactly one `playing` row exists afterward, regardless of which
        # response "saw" the transition -- the expired item is `played`
        # exactly once, never re-expired or double-promoted.
        playing_rows = music_station_db.dal(
            music_station_db.dal.music_station_queue.status == "playing"
        ).select()
        assert len(playing_rows) == 1
        assert playing_rows.first().id == next_id

        expired_row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == expired_id)
            .select()
            .first()
        )
        assert expired_row.status == "played"

    async def test_eta_seconds_for_queued_items(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        playing_track = _seed_track(music_station_db, duration_ms=600000)  # far from expiring
        _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=playing_track,
            status="playing",
            started_at=datetime.now(UTC),
        )
        queued_track_1 = _seed_track(music_station_db, duration_ms=210000)
        queued_track_2 = _seed_track(music_station_db, duration_ms=180000)
        first_queued_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=queued_track_1,
            status="queued",
            position=1,
        )
        second_queued_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=queued_track_2,
            status="queued",
            position=2,
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["now_playing"]["eta_seconds"] is None

        by_id = {item["id"]: item for item in body["data"]["queue"]}
        # First queued item: ~all of the still-playing track's remaining time.
        assert 590 <= by_id[first_queued_id]["eta_seconds"] <= 600
        # Second queued item: first item's ETA + the first item's own duration (210s).
        assert by_id[second_queued_id]["eta_seconds"] == by_id[first_queued_id]["eta_seconds"] + 210

    async def test_requested_by_resolves_display_name_from_community_members(
        self, client: Any, music_station_db: Any
    ) -> None:
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
            status="queued",
            requested_by=7,
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        body = await response.get_json()
        item = body["data"]["now_playing"]  # auto-started (nothing else playing)
        assert item["requested_by"] == {"display_name": "PenguinFan42", "platform": "twitch"}

    async def test_requested_by_falls_back_when_unresolvable(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db)
        _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="queued",
            requested_by=None,
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        body = await response.get_json()
        item = body["data"]["now_playing"]
        assert item["requested_by"] == {"display_name": "platform user", "platform": "unknown"}


class TestLiveQueueAdvance:
    """`POST /api/v1/internal/music/queue/advance` -- guarded advance, service-key only."""

    async def test_missing_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _LIVE_ADVANCE_ROUTE, json={"community_id": community_id, "item_id": 1}
        )
        assert response.status_code == 401

    async def test_missing_params_is_400(self, client: Any) -> None:
        response = await client.post(
            _LIVE_ADVANCE_ROUTE, headers={"X-Service-Key": SERVICE_API_KEY}, json={}
        )
        assert response.status_code == 400

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.post(
            _LIVE_ADVANCE_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": 999999, "item_id": 1},
        )
        assert response.status_code == 404

    async def test_advances_when_item_id_matches_current_playing(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        playing_track = _seed_track(music_station_db)
        playing_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=playing_track,
            status="playing",
            started_at=datetime.now(UTC),
        )
        next_track = _seed_track(music_station_db)
        next_id = _seed_queue_item(
            music_station_db, community_id=community_id, track_id=next_track, status="queued"
        )

        response = await client.post(
            _LIVE_ADVANCE_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "item_id": playing_id},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["advanced"] is True
        assert body["data"]["now_playing"]["id"] == next_id

        old_row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == playing_id)
            .select()
            .first()
        )
        assert old_row.status == "played"

    async def test_stale_item_id_does_not_advance(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        playing_track = _seed_track(music_station_db)
        playing_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=playing_track,
            status="playing",
            started_at=datetime.now(UTC),
        )

        response = await client.post(
            _LIVE_ADVANCE_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "item_id": playing_id + 999},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["advanced"] is False
        assert body["data"]["now_playing"]["id"] == playing_id

        row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == playing_id)
            .select()
            .first()
        )
        assert row.status == "playing"

    async def test_advance_with_no_playing_item_does_not_advance(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _LIVE_ADVANCE_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "item_id": 1},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["advanced"] is False
        assert body["data"]["now_playing"] is None


def _playback_key(community_id: int) -> str:
    """Mirrors `services.community_music_queue_service._playback_key()` -- gh-315 contract."""
    return f"music:playback:{community_id}"


class TestPlayback:
    """`POST /api/v1/internal/music/playback` -- pause/resume (gh-315).

    Fail-first proof (executed, not narrated): temporarily made
    `set_playback()` skip the `current.paused` check entirely (always
    treat `pause` as a fresh pause) -- `test_double_pause_is_already_
    paused` went red (`changed=True`/`reason="paused"` on the second call
    instead of `changed=False`/`reason="already_paused"`); reverted,
    green again.
    """

    async def test_pause_then_resume_round_trip(
        self, client: Any, app: Quart, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=200_000)
        queue_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC),
        )

        pause_response = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "pause"},
        )
        assert pause_response.status_code == 200
        pause_body = (await pause_response.get_json())["data"]
        assert pause_body["changed"] is True
        assert pause_body["reason"] == "paused"
        assert pause_body["paused"] is True
        assert pause_body["paused_since"] is not None
        assert pause_body["now_playing"]["id"] == queue_id
        assert pause_body["community_id"] == community_id

        redis_client = app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY]
        assert await redis_client.get(_playback_key(community_id)) is not None

        resume_response = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "resume"},
        )
        assert resume_response.status_code == 200
        resume_body = (await resume_response.get_json())["data"]
        assert resume_body["changed"] is True
        assert resume_body["reason"] == "resumed"
        assert resume_body["paused"] is False
        assert resume_body["paused_since"] is None

        # Idempotent no-op: resuming again when already playing changes nothing.
        second_resume = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "resume"},
        )
        second_body = (await second_resume.get_json())["data"]
        assert second_body["changed"] is False
        assert second_body["reason"] == "already_playing"

    async def test_double_pause_is_already_paused(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=200_000)
        _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC),
        )

        first = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "pause"},
        )
        assert (await first.get_json())["data"]["reason"] == "paused"

        second = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "pause"},
        )
        second_body = (await second.get_json())["data"]
        assert second_body["changed"] is False
        assert second_body["reason"] == "already_paused"
        assert second_body["paused"] is True

    async def test_nothing_playing_pause_and_resume(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)

        for action in ("pause", "resume"):
            response = await client.post(
                _PLAYBACK_ROUTE,
                headers={"X-Service-Key": SERVICE_API_KEY},
                json={"community_id": community_id, "action": action},
            )
            assert response.status_code == 200
            body = (await response.get_json())["data"]
            assert body["reason"] == "nothing_playing"
            assert body["changed"] is False
            assert body["paused"] is False
            assert body["now_playing"] is None
            assert body["position_ms"] is None

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": 999999, "action": "pause"},
        )
        assert response.status_code == 404

    async def test_bad_action_is_400(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "stop"},
        )
        assert response.status_code == 400

    async def test_missing_service_key_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.post(
            _PLAYBACK_ROUTE, json={"community_id": community_id, "action": "pause"}
        )
        assert response.status_code == 401


class TestPlaybackTimeMath:
    """Position/ETA math + auto-advance interaction with a paused clock (gh-315)."""

    async def test_position_ms_reflects_prior_pause_duration(
        self, client: Any, app: Quart, music_station_db: Any
    ) -> None:
        """Known-length pause: 100s since `started_at`, 30s of that was paused -> ~70s position."""
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=600_000)
        _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC) - timedelta(seconds=100),
        )
        redis_client = app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY]
        await redis_client.set(
            _playback_key(community_id),
            (
                '{"paused_since": null, "paused_total_ms": 30000, '
                f'"updated_at": "{datetime.now(UTC).isoformat()}"}}'
            ),
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        playback = (await response.get_json())["data"]["playback"]
        assert playback["paused"] is False
        # ~100s elapsed - 30s paused == ~70s; generous tolerance for test wall-clock drift.
        assert 65_000 <= playback["position_ms"] <= 75_000

    async def test_auto_advance_does_not_fire_while_paused_past_duration(
        self, client: Any, app: Quart, music_station_db: Any
    ) -> None:
        """Raw wall-clock time since `started_at` is WAY past duration+grace -- but paused."""
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=1000)  # 1s track
        playing_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        redis_client = app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY]
        # Currently paused, and has been for 25s -- pause-adjusted elapsed is
        # only ~5s (30s wall-clock - 25s paused), well under duration(1s) +
        # grace(10s) = 11s, so this must NOT auto-advance.
        paused_since = (datetime.now(UTC) - timedelta(seconds=25)).isoformat()
        await redis_client.set(
            _playback_key(community_id),
            f'{{"paused_since": "{paused_since}", "paused_total_ms": 0, '
            f'"updated_at": "{datetime.now(UTC).isoformat()}"}}',
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["now_playing"]["id"] == playing_id
        assert body["data"]["now_playing"]["status"] == "playing"
        assert body["data"]["playback"]["paused"] is True

        row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == playing_id)
            .select()
            .first()
        )
        assert row.status == "playing"  # unchanged in the DB, not just the response

    async def test_auto_advance_fires_once_elapsed_reaches_duration_after_resume(
        self, client: Any, app: Quart, music_station_db: Any
    ) -> None:
        """Same 30s wall-clock gap, but resumed with only 5s of accumulated pause -> expired."""
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db, duration_ms=1000)  # 1s track
        expired_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        next_track_id = _seed_track(music_station_db)
        next_id = _seed_queue_item(
            music_station_db, community_id=community_id, track_id=next_track_id, status="queued"
        )
        redis_client = app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY]
        # Not currently paused (resumed) -- pause-adjusted elapsed is ~25s
        # (30s wall-clock - 5s paused_total_ms), past duration(1s) + grace(10s).
        await redis_client.set(
            _playback_key(community_id),
            (
                '{"paused_since": null, "paused_total_ms": 5000, '
                f'"updated_at": "{datetime.now(UTC).isoformat()}"}}'
            ),
        )

        response = await client.get(
            f"{_LIVE_QUEUE_ROUTE}?community_id={community_id}",
            headers={"X-Service-Key": SERVICE_API_KEY},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["now_playing"]["id"] == next_id
        assert body["data"]["playback"]["paused"] is False

        expired_row = (
            music_station_db.dal(music_station_db.dal.music_station_queue.id == expired_id)
            .select()
            .first()
        )
        assert expired_row.status == "played"

    async def test_advance_resets_playback_key(
        self, client: Any, app: Quart, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        track_id = _seed_track(music_station_db)
        playing_id = _seed_queue_item(
            music_station_db,
            community_id=community_id,
            track_id=track_id,
            status="playing",
            started_at=datetime.now(UTC),
        )
        next_track_id = _seed_track(music_station_db)
        _seed_queue_item(
            music_station_db, community_id=community_id, track_id=next_track_id, status="queued"
        )

        pause_response = await client.post(
            _PLAYBACK_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "action": "pause"},
        )
        assert (await pause_response.get_json())["data"]["paused"] is True
        redis_client = app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY]
        assert await redis_client.get(_playback_key(community_id)) is not None

        advance_response = await client.post(
            _LIVE_ADVANCE_ROUTE,
            headers={"X-Service-Key": SERVICE_API_KEY},
            json={"community_id": community_id, "item_id": playing_id},
        )
        assert advance_response.status_code == 200
        advance_body = await advance_response.get_json()
        assert advance_body["data"]["advanced"] is True
        assert advance_body["data"]["playback"]["paused"] is False

        # The key itself is gone, not just reported unpaused -- the next
        # track starts with a completely clean slate.
        assert await redis_client.get(_playback_key(community_id)) is None
