"""`blueprints/v1/community_music_queue.py` -- Music Station queue/policy/moderation tests.

Standalone Quart app registering only `music_queue_bp` against the
`music_station_db` fixture (`tests/conftest.py`) -- real JWTs via
`flask_core.auth.create_jwt_token`, real pydal queries, no mocking of the
authz chain (mirrors `tests/test_v1_music_blueprint.py`'s own pattern,
the closest sibling feature in this repo).

Provider resolution: `services.music_providers.resolve()` (PR #225,
merged to `release/v3.0.X`) makes REAL YouTube Data API v3 / Spotify Web
API network calls -- exactly right for production, wrong for this
module's own fast/deterministic queue/policy/moderation test suite. Every
test that needs a successful enqueue uses the `fake_resolve` fixture
below (monkeypatches `services.community_music_queue_service.resolve`
only, never the shared `services.music_providers` package itself) --
provider-resolution-specific coverage (real network mocking, credential
handling, `ProviderUnavailable`/`TrackNotFound` mapping) lives in PR
#225's own `tests/test_music_providers.py`.
`test_enqueue_unsupported_provider_is_422` is the one exception: it calls
the REAL (unmocked) `resolve()` with a provider name outside
`{"youtube", "spotify"}`, which raises `TrackNotFound` with no network
call at all (`services/music_providers/__init__.py::resolve()`'s own
`_KNOWN_PROVIDERS` check runs before any dispatch) -- deterministic and
worth proving end-to-end without a mock.

Every test seeds ALL sync (`music_station_db.dal.*.insert()`) fixtures
BEFORE making any async `client.*()` call, and verifies post-write state
either via the route's own JSON response or via `music_station_db.
select_async(...)` (same executor connection as the write) rather than a
bare sync `dal(...).select()` -- `hub_api/PORTING.md` Gotcha #2's
`pool_size=1` sqlite file: a sync write/read on the main thread's own
connection while the async executor thread's connection still holds an
open, uncommitted transaction (`insert_async`/`update_async` never call
`.commit()`) raises `sqlite3.OperationalError: database is locked`,
caught the hard way while first writing this file's moderation tests.

Fail-first proof (executed, not narrated) for the four gates this group
adds on top of the already-authz-proven `services.community_authz`
chain (that module's own fail-first coverage lives in
`tests/test_v1_music_blueprint.py`'s module docstring -- not re-derived
here):

1. `test_song_requests_disabled_rejects_enqueue` -- temporarily made
   `_enforce_request_policy` return immediately without checking
   `policy.songRequestsAllowed` -- test went red (201 instead of 403,
   requests silently allowed with the policy switched off); reverted,
   green again.
2. `test_category_restricted_rejects_offline_request` -- temporarily
   made `_enforce_request_policy` skip the
   `policy.requestsCategoryRestricted` branch entirely -- test went red
   (201 instead of 422, a category-restricted community accepting
   requests while offline); reverted, green again.
3. `test_kick_song_requires_admin_is_403` -- temporarily called
   `authorize_community(..., admin=False)` instead of `admin=True` in
   `kick_song()` -- test went red (200 instead of 403, any member could
   kick another member's song); reverted, green again.
4. `test_cross_tenant_community_is_403` -- covered by
   `services.community_authz.resolve_community_membership_scoped`'s own
   tenant-ownership check, already fail-first-proven in
   `tests/test_v1_music_blueprint.py::TestCommunityAdminGate::
   test_community_in_different_tenant_is_403`; this test exercises the
   same shared code path through this group's own routes as an
   integration check, not a second derivation of that proof.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_music_queue as community_music_queue_module
from blueprints.v1.community_music_queue import MUSIC_PLAYBACK_REDIS_CONFIG_KEY, music_queue_bp
from config import HubAPIConfig
from services.music_providers.track import Track
from tests.conftest import TENANT_SLUG, make_user_token, make_user_token_with_roles


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
        default_tenant_slug=DEFAULT_TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8205",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(music_station_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
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
    """Deterministic stand-in for the real, network-calling `resolve()` -- see module docstring."""
    _set_fake_resolve(monkeypatch)


def _set_fake_resolve(
    monkeypatch: Any, *, track_provider: str = "youtube", labels: tuple[str, ...] = ()
) -> None:
    """Same fake `resolve()` as the `fake_resolve` fixture, with configurable provider/labels.

    Used directly (not as a fixture) by `TestYoutubeLabelGate` (gh-313),
    which needs a different `Track.labels`/`provider` per test case.
    """

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


def _tenant_id(db: Any) -> int:
    row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    return int(row.id)


def _seed_community(db: Any, *, tenant_id: int | None = None) -> int:
    tid = tenant_id if tenant_id is not None else _tenant_id(db)
    community_id: int = db.dal.communities.insert(name="acme-community", tenant_id=tid)
    db.dal.commit()
    return community_id


def _seed_member(
    db: Any,
    *,
    community_id: int,
    user_id: int,
    role: str = "member",
    scopes: list[str] | None = None,
) -> None:
    role_id = None
    if scopes is not None:
        role_id = db.dal.community_roles.insert(
            community_id=community_id, name=role, base_claims={"scopes": scopes}
        )
    db.dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role=role,
        community_role_id=role_id,
        is_active=True,
    )
    db.dal.commit()


def _admin_headers(db: Any, *, community_id: int, user_id: int = 1) -> dict[str, str]:
    _seed_member(
        db,
        community_id=community_id,
        user_id=user_id,
        role="community-admin",
        scopes=["community:manage_members"],
    )
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


def _member_headers(db: Any, *, community_id: int, user_id: int = 2) -> dict[str, str]:
    _seed_member(
        db,
        community_id=community_id,
        user_id=user_id,
        role="member",
        scopes=["community:read"],
    )
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


def _set_live_category(db: Any, *, community_id: int, game_name: str) -> None:
    db.dal.community_servers.insert(
        community_id=community_id,
        platform="twitch",
        platform_server_id="chan-1",
        status="active",
    )
    db.dal.coordination.insert(
        entity_id=str(community_id),
        platform="twitch",
        server_id="chan-1",
        is_live=True,
        game_name=game_name,
    )
    db.dal.commit()


async def _select_async(db: Any, query: Any) -> Any:
    """Read via the SAME connection async writes in this test used -- see module docstring."""
    return await db.select_async(db.dal(query))


# ---------------------------------------------------------------------------
# Auth bypass
# ---------------------------------------------------------------------------


class TestAuthBypass:
    async def test_missing_token_is_401(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        response = await client.get(f"/api/v1/admin/{community_id}/music-station/queue")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestPolicy:
    async def test_get_policy_creates_default(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/policy", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["policy"]["songRequestsAllowed"] is True
        assert body["policy"]["requestsCategoryRestricted"] is False

    async def test_get_policy_member_is_403(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/policy", headers=headers
        )
        assert response.status_code == 403

    async def test_set_policy_persists(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"songRequestsAllowed": False, "requestsCategoryRestricted": True},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["policy"]["songRequestsAllowed"] is False
        assert body["policy"]["requestsCategoryRestricted"] is True

    async def test_set_policy_no_fields_is_400(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy", headers=headers, json={}
        )
        assert response.status_code == 400

    # regression: gh-313

    async def test_get_policy_default_youtube_allowed_labels_is_empty(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/policy", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["policy"]["youtubeAllowedLabels"] == []

    async def test_set_policy_youtube_allowed_labels_round_trip_normalizes(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"youtubeAllowedLabels": [" Music ", "MUSIC", "Gaming"]},
        )
        assert response.status_code == 200
        body = await response.get_json()
        # Trimmed, lowercased, de-duplicated, order of first appearance kept.
        assert body["policy"]["youtubeAllowedLabels"] == ["music", "gaming"]

        get_response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/policy", headers=headers
        )
        get_body = await get_response.get_json()
        assert get_body["policy"]["youtubeAllowedLabels"] == ["music", "gaming"]

    async def test_set_policy_youtube_allowed_labels_clear_with_empty_list(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"youtubeAllowedLabels": ["music"]},
        )
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"youtubeAllowedLabels": []},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["policy"]["youtubeAllowedLabels"] == []

    async def test_set_policy_youtube_allowed_labels_too_many_is_400(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"youtubeAllowedLabels": [f"label{i}" for i in range(33)]},
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert body["error"]["message"] == "youtube-labels: up to 32 labels, 64 chars each"

    async def test_set_policy_youtube_allowed_labels_too_long_entry_is_400(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"youtubeAllowedLabels": ["a" * 65]},
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert body["error"]["message"] == "youtube-labels: up to 32 labels, 64 chars each"

    async def test_get_policy_invalid_json_column_returns_empty_list(
        self, client: Any, music_station_db: Any
    ) -> None:
        """A corrupted/legacy `youtube_allowed_labels` value degrades to `[]`, never a 500."""
        community_id = _seed_community(music_station_db)
        headers = _admin_headers(music_station_db, community_id=community_id)
        # Create the default row first.
        await client.get(f"/api/v1/admin/{community_id}/music-station/policy", headers=headers)
        music_station_db.dal(music_station_db.dal.music_policy.community_id == community_id).update(
            youtube_allowed_labels="not valid json"
        )
        music_station_db.dal.commit()

        response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/policy", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["policy"]["youtubeAllowedLabels"] == []


# ---------------------------------------------------------------------------
# Enqueue: policy gate, category gate, provider resolution
# ---------------------------------------------------------------------------


class TestEnqueueRequest:
    async def test_song_requests_disabled_rejects_enqueue(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)

        set_response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=admin_headers,
            json={"songRequestsAllowed": False},
        )
        assert set_response.status_code == 200

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 403

    async def test_category_restricted_rejects_offline_request(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        # No live category row seeded at all -- not live under "Music" -> rejected.

        set_response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=admin_headers,
            json={"requestsCategoryRestricted": True},
        )
        assert set_response.status_code == 200

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 422

    async def test_category_restricted_allows_when_live_music(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        _set_live_category(music_station_db, community_id=community_id, game_name="Music")

        await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=admin_headers,
            json={"requestsCategoryRestricted": True},
        )
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 201

    async def test_admin_override_bypasses_category_restriction(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)

        await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=admin_headers,
            json={"requestsCategoryRestricted": True},
        )
        # Offline / wrong category -- would normally 422 for a member.
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=admin_headers,
            json={
                "urlOrQuery": "https://www.youtube.com/watch?v=abc123",
                "overrideCategoryRestriction": True,
            },
        )
        assert response.status_code == 201

        log_rows = await _select_async(
            music_station_db,
            music_station_db.dal.music_moderation_log.action == "category_override",
        )
        assert len(log_rows) == 1

    async def test_enqueue_success_via_mocked_provider(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        """Real queue insert/dedup/DTO-mapping path -- provider resolution itself is mocked."""
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["item"]["track"]["provider"] == "youtube"
        assert body["item"]["track"]["externalId"] == "https://www.youtube.com/watch?v=abc123"
        assert body["item"]["track"]["artist"] == "Test Artist"
        assert body["item"]["status"] == "queued"

    async def test_enqueue_unsupported_provider_is_422(
        self, client: Any, music_station_db: Any
    ) -> None:
        """Real (unmocked) `resolve()` -- an unsupported provider is a real, deterministic 422."""
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=headers,
            json={"urlOrQuery": "some song request", "provider": "soundcloud"},
        )
        assert response.status_code == 422
        body = await response.get_json()
        assert "no track found" in body["error"]["message"].lower()

    async def test_enqueue_missing_url_is_400(self, client: Any, music_station_db: Any) -> None:
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=headers,
            json={"urlOrQuery": "   "},
        )
        assert response.status_code == 400


class TestYoutubeLabelGate:
    """gh-313: `youtube_allowed_labels` community allowlist enforcement on enqueue."""

    async def _set_allowed_labels(
        self, client: Any, headers: dict[str, str], community_id: int, labels: list[str]
    ) -> None:
        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/policy",
            headers=headers,
            json={"youtubeAllowedLabels": labels},
        )
        assert response.status_code == 200

    async def test_empty_allowlist_accepts_any_youtube_track(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, labels=("comedy",))
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 201

    async def test_exact_label_match_accepts(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, labels=("music", "comedy"))
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        await self._set_allowed_labels(client, admin_headers, community_id, ["music"])

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 201

    async def test_whole_word_match_in_multiword_label_accepts(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        """Allowed `"music"` matches multi-word track label `"rock music"` as a whole word."""
        _set_fake_resolve(monkeypatch, labels=("rock music",))
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        await self._set_allowed_labels(client, admin_headers, community_id, ["music"])

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 201

    async def test_substring_without_word_boundary_does_not_match(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        """`"music"` inside `"musical"` is a substring, not a whole word -- must reject."""
        _set_fake_resolve(monkeypatch, labels=("musical",))
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        await self._set_allowed_labels(client, admin_headers, community_id, ["music"])

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 422
        body = await response.get_json()
        assert body["error"]["code"] == "youtube_label_not_allowed"

    async def test_no_match_rejected_with_code_and_exact_message(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, labels=("comedy",))
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        await self._set_allowed_labels(client, admin_headers, community_id, ["a", "b", "c"])

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 422
        body = await response.get_json()
        assert body["error"]["code"] == "youtube_label_not_allowed"
        assert body["error"]["message"] == "that video isn't allowed here (allowed: a, b, c)"

    async def test_spotify_track_bypasses_label_gate(
        self, client: Any, music_station_db: Any, monkeypatch: Any
    ) -> None:
        _set_fake_resolve(monkeypatch, track_provider="spotify", labels=())
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        await self._set_allowed_labels(client, admin_headers, community_id, ["music"])

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://open.spotify.com/track/abc123"},
        )
        assert response.status_code == 201

    async def test_flag_off_bypasses_label_gate(
        self,
        client: Any,
        music_station_db: Any,
        monkeypatch: Any,
        _youtube_labels_flag_default_on: AsyncMock,
    ) -> None:
        _youtube_labels_flag_default_on.return_value = False
        _set_fake_resolve(monkeypatch, labels=("comedy",))
        community_id = _seed_community(music_station_db)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=1)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        await self._set_allowed_labels(client, admin_headers, community_id, ["music"])

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=abc123"},
        )
        assert response.status_code == 201


class TestEnqueuePlaylist:
    async def test_enqueue_playlist_groups_under_one_playlist_id(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/playlists",
            headers=headers,
            json={
                "items": [
                    "https://www.youtube.com/watch?v=one",
                    "https://www.youtube.com/watch?v=two",
                ]
            },
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert len(body["items"]) == 2
        assert body["items"][0]["playlistId"] == body["playlistId"]
        assert body["items"][1]["playlistId"] == body["playlistId"]

    async def test_enqueue_playlist_empty_items_is_400(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/playlists",
            headers=headers,
            json={"items": []},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# List / lifecycle
# ---------------------------------------------------------------------------


class TestQueueLifecycle:
    async def _enqueue(
        self, client: Any, headers: dict[str, str], community_id: int, url: str
    ) -> Any:
        return await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=headers,
            json={"urlOrQuery": url},
        )

    async def test_list_queue_now_playing_and_upcoming(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id)
        await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=a"
        )
        await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=b"
        )

        list_response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/queue", headers=member_headers
        )
        assert list_response.status_code == 200
        body = await list_response.get_json()
        assert body["nowPlaying"] is None
        assert len(body["upcoming"]) == 2
        assert body["upcoming"][0]["position"] < body["upcoming"][1]["position"]

    async def test_advance_promotes_next_track(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=99)

        await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=a"
        )
        await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=b"
        )

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/advance", headers=admin_headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["previous"] is None
        assert body["next"]["status"] == "playing"

        list_response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/queue", headers=member_headers
        )
        list_body = await list_response.get_json()
        assert list_body["nowPlaying"]["status"] == "playing"
        assert len(list_body["upcoming"]) == 1

    async def test_reorder_queue(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=99)

        first = await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=a"
        )
        second = await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=b"
        )
        first_id = (await first.get_json())["item"]["id"]
        second_id = (await second.get_json())["item"]["id"]

        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/queue/reorder",
            headers=admin_headers,
            json={"orderedQueueIds": [second_id, first_id]},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["upcoming"][0]["id"] == second_id
        assert body["upcoming"][1]["id"] == first_id

    async def test_reorder_mismatched_ids_is_400(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=99)

        await self._enqueue(
            client, member_headers, community_id, "https://www.youtube.com/watch?v=a"
        )

        response = await client.put(
            f"/api/v1/admin/{community_id}/music-station/queue/reorder",
            headers=admin_headers,
            json={"orderedQueueIds": [999999]},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------


class TestModeration:
    async def test_kick_song_requires_admin_is_403(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        other_member_headers = _member_headers(
            music_station_db, community_id=community_id, user_id=3
        )

        enqueue_response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=a"},
        )
        queue_id = (await enqueue_response.get_json())["item"]["id"]

        response = await client.delete(
            f"/api/v1/admin/{community_id}/music-station/queue/{queue_id}",
            headers=other_member_headers,
        )
        assert response.status_code == 403

    async def test_kick_song_by_admin_removes_and_logs(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=99)

        enqueue_response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=member_headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=a"},
        )
        queue_id = (await enqueue_response.get_json())["item"]["id"]

        response = await client.delete(
            f"/api/v1/admin/{community_id}/music-station/queue/{queue_id}?reason=spam",
            headers=admin_headers,
        )
        assert response.status_code == 200

        queue_rows = await _select_async(
            music_station_db, music_station_db.dal.music_station_queue.id == queue_id
        )
        assert queue_rows.first().status == "removed"

        log_rows = await _select_async(
            music_station_db, music_station_db.dal.music_moderation_log.action == "kick_song"
        )
        assert len(log_rows) == 1
        assert log_rows.first().reason == "spam"

    async def test_kick_playlist_removes_all_queued_entries(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        community_id = _seed_community(music_station_db)
        member_headers = _member_headers(music_station_db, community_id=community_id, user_id=2)
        admin_headers = _admin_headers(music_station_db, community_id=community_id, user_id=99)

        playlist_response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/playlists",
            headers=member_headers,
            json={
                "items": [
                    "https://www.youtube.com/watch?v=a",
                    "https://www.youtube.com/watch?v=b",
                ]
            },
        )
        playlist_id = (await playlist_response.get_json())["playlistId"]

        response = await client.delete(
            f"/api/v1/admin/{community_id}/music-station/queue/playlists/{playlist_id}",
            headers=admin_headers,
        )
        assert response.status_code == 200

        remaining = await _select_async(
            music_station_db,
            (music_station_db.dal.music_station_queue.playlist_id == playlist_id)
            & (music_station_db.dal.music_station_queue.status == "queued"),
        )
        assert len(remaining) == 0

        log_rows = await _select_async(
            music_station_db, music_station_db.dal.music_moderation_log.action == "kick_playlist"
        )
        assert len(log_rows) == 1


# ---------------------------------------------------------------------------
# Cross-tenant IDOR
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    async def test_cross_tenant_community_is_403(self, client: Any, music_station_db: Any) -> None:
        """A caller admin-scoped in a DIFFERENT tenant's community must not reach this one."""
        other_tenant_id = music_station_db.dal.tenants.insert(slug="other-tenant", is_active=True)
        music_station_db.dal.commit()
        community_id = _seed_community(music_station_db, tenant_id=other_tenant_id)
        _seed_member(
            music_station_db,
            community_id=community_id,
            user_id=1,
            role="community-admin",
            scopes=["community:manage_members"],
        )
        # Caller's JWT carries the DEFAULT tenant, not `other-tenant`.
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1, tenant=TENANT_SLUG)}"}
        response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/queue", headers=headers
        )
        assert response.status_code == 403

    async def test_super_admin_role_claim_bypasses(
        self, client: Any, music_station_db: Any
    ) -> None:
        community_id = _seed_community(music_station_db)
        headers = {
            "Authorization": (
                f"Bearer {make_user_token_with_roles(user_id=99, roles=['super_admin'])}"
            )
        }
        response = await client.get(
            f"/api/v1/admin/{community_id}/music-station/queue", headers=headers
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Regression: eta_seconds naive started_at
# ---------------------------------------------------------------------------


class TestEtaSecondsNaiveStartedAt:
    async def test_enqueue_with_playing_row_computes_sane_eta(
        self, client: Any, music_station_db: Any, fake_resolve: None
    ) -> None:
        """# regression: eta naive started_at.

        `pydal` returns a NAIVE `datetime` on read even for a value
        written via `datetime.now(UTC)` (aware) -- `_compute_eta_seconds()`
        used to subtract that naive `started_at` straight from
        `datetime.now(UTC)`, raising `TypeError: can't subtract
        offset-naive and offset-aware datetimes` any time a `playing` row
        existed. Seeds one `playing` row (~50s into a 200s track) and one
        `queued` row ahead of the new request (a 100s track) -- the new
        item's `etaSeconds` must be the playing row's remaining time
        (~150s) plus the queued-ahead track's full duration (100s), never
        a 500.
        """
        community_id = _seed_community(music_station_db)
        tenant_id = _tenant_id(music_station_db)
        headers = _member_headers(music_station_db, community_id=community_id)

        playing_track_id = music_station_db.dal.music_tracks.insert(
            tenant_id=tenant_id,
            provider="youtube",
            external_id="now-playing",
            title="Now Playing",
            artist="Artist",
            duration_ms=200_000,
            artwork_url=None,
            url="https://www.youtube.com/watch?v=nowplaying",
            created_at=datetime.now(UTC),
        )
        queued_track_id = music_station_db.dal.music_tracks.insert(
            tenant_id=tenant_id,
            provider="youtube",
            external_id="queued-ahead",
            title="Queued Ahead",
            artist="Artist",
            duration_ms=100_000,
            artwork_url=None,
            url="https://www.youtube.com/watch?v=queuedahead",
            created_at=datetime.now(UTC),
        )
        music_station_db.dal.music_station_queue.insert(
            tenant_id=tenant_id,
            community_id=community_id,
            track_id=playing_track_id,
            position=0,
            status="playing",
            source="request",
            added_at=datetime.now(UTC) - timedelta(seconds=50),
            started_at=datetime.now(UTC) - timedelta(seconds=50),
        )
        music_station_db.dal.music_station_queue.insert(
            tenant_id=tenant_id,
            community_id=community_id,
            track_id=queued_track_id,
            position=1,
            status="queued",
            source="request",
            added_at=datetime.now(UTC),
        )
        music_station_db.dal.commit()

        response = await client.post(
            f"/api/v1/admin/{community_id}/music-station/queue/requests",
            headers=headers,
            json={"urlOrQuery": "https://www.youtube.com/watch?v=newrequest"},
        )

        assert response.status_code == 201
        body = await response.get_json()
        eta_seconds = body["item"]["etaSeconds"]
        # ~150s remaining on the playing track + 100s queued ahead == ~250s;
        # generous tolerance absorbs normal test-execution wall-clock drift.
        assert 200 <= eta_seconds <= 255
