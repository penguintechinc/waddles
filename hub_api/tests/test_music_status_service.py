"""Tests for `services/music_status_service.py`'s Spotify + YouTube health probes.

Same mocked-transport approach as `test_music_providers.py`/
`test_music_providers_youtube_oauth.py`: `httpx.MockTransport` swapped in
for `httpx.AsyncClient` (module-global, so it intercepts both this module's
own `httpx.AsyncClient(...)` calls AND `services.music_providers.youtube`'s
reused auth/token-fetch calls), so request building and status-code
interpretation all run for real -- no real socket ever touched.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from services import music_status_service as status_mod
from services.community_connections import DecryptedTokens
from services.music_providers import youtube as youtube_mod

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport) -> Callable[..., httpx.AsyncClient]:
    """Build a replacement for `httpx.AsyncClient` that always uses `transport`."""

    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=transport)

    return factory


def _use_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test relies on a real `~/.spotify.token`/`~/.youtube.token` or ambient env var."""
    for var in (
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "YOUTUBE_API_KEY",
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        status_mod, "_TOKEN_FILE", status_mod._TOKEN_FILE.parent / "__nope_spotify__.token"
    )
    monkeypatch.setattr(
        youtube_mod, "_TOKEN_FILE", youtube_mod._TOKEN_FILE.parent / "__nope_youtube__.token"
    )


@pytest.fixture(autouse=True)
def _reset_module_caches() -> Any:
    """Every probe/token/community-auth cache here is module-level state -- isolate every test."""
    status_mod._health_cache = None
    status_mod._youtube_health_cache = {}
    youtube_mod._oauth_token_cache = None
    youtube_mod._community_auth_cache = {}
    yield
    status_mod._health_cache = None
    status_mod._youtube_health_cache = {}
    youtube_mod._oauth_token_cache = None
    youtube_mod._community_auth_cache = {}


def _oauth_trio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "test-refresh-token")


class TestYoutubeHealthNotConfigured:
    """No YouTube credentials at all -- `not_configured`, never a network call."""

    async def test_no_credentials_returns_not_configured_without_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call should happen with no credentials")

        _use_transport(monkeypatch, boom)

        health = await status_mod.check_youtube_health()

        assert health.state == "not_configured"
        assert health.cause == "youtube credentials not configured"
        assert health.source == "none"


class TestYoutubeHealthApiKeyMode:
    """`YOUTUBE_API_KEY` credential mode -- the common case."""

    async def test_healthy_probe_returns_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health()

        assert health.state == "enabled"
        assert health.cause is None
        assert health.source == "api_key"
        assert seen_params["id"] == "dQw4w9WgXcQ"
        assert seen_params["key"] == "test-key"

    async def test_401_returns_error_with_status_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "bad-key")
        _use_transport(monkeypatch, lambda _r: httpx.Response(401, json={"error": {}}))

        health = await status_mod.check_youtube_health()

        assert health.state == "error"
        assert health.cause == "youtube oauth token didn't work (401)"

    async def test_403_quota_error_surfaces_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"error": {"errors": [{"reason": "quotaExceeded", "message": "..."}]}},
            )

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health()

        assert health.state == "error"
        assert health.cause == "youtube quota/access error (quotaExceeded)"

    async def test_5xx_returns_generic_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        _use_transport(monkeypatch, lambda _r: httpx.Response(503, json={}))

        health = await status_mod.check_youtube_health()

        assert health.state == "error"
        assert health.cause == "youtube api error (503)"

    async def test_network_error_returns_unreachable_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health()

        assert health.state == "error"
        assert health.cause == "youtube unreachable: ConnectError"


class TestYoutubeHealthOAuthMode:
    """`YOUTUBE_CLIENT_ID`+`YOUTUBE_CLIENT_SECRET`+`YOUTUBE_REFRESH_TOKEN` credential mode."""

    async def test_healthy_probe_uses_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _oauth_trio(monkeypatch)
        seen_auth_header = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_auth_header
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            seen_auth_header = request.headers.get("Authorization")
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health()

        assert health.state == "enabled"
        assert health.source == "env"
        assert seen_auth_header == "Bearer tok"

    async def test_refresh_exchange_failure_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _oauth_trio(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "oauth2.googleapis.com"
            return httpx.Response(400, json={"error": "invalid_grant"})

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health()

        assert health.state == "error"
        assert health.cause is not None
        assert health.cause.startswith("youtube oauth token didn't work: ")


class TestYoutubeHealthCaching:
    """`check_youtube_health()`'s own >=60s in-process cache."""

    async def test_second_call_within_ttl_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        await status_mod.check_youtube_health()
        await status_mod.check_youtube_health()

        assert call_count == 1

    async def test_call_after_ttl_expiry_reprobes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        await status_mod.check_youtube_health()
        assert status_mod._youtube_health_cache.get(None) is not None
        stale = time.monotonic() - status_mod._CACHE_TTL_SECONDS - 1
        status_mod._youtube_health_cache[None].checked_at = stale

        await status_mod.check_youtube_health()

        assert call_count == 2


class TestYoutubeHealthCommunitySource:
    """`check_youtube_health(db, community_id)`'s `source` field (issue #320)."""

    async def test_community_connected_reports_community_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-fallback-key")

        async def fake_get_decrypted_tokens(
            _db: Any, community_id: int, provider: str, *, tenant_id: Any
        ) -> Any:
            assert community_id == 42
            assert provider == "youtube"
            return DecryptedTokens(
                access_token="community-token",
                refresh_token=None,
                expires_at=None,
                scopes=[],
            )

        monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", fake_get_decrypted_tokens)

        seen_auth_header = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_auth_header
            seen_auth_header = request.headers.get("Authorization")
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health(db=object(), community_id=42)

        assert health.state == "enabled"
        assert health.source == "community"
        assert seen_auth_header == "Bearer community-token"

    async def test_no_community_connection_falls_back_to_env_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")

        async def fake_get_decrypted_tokens(
            _db: Any, _community_id: int, _provider: str, *, tenant_id: Any
        ) -> Any:
            return None

        monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", fake_get_decrypted_tokens)

        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        health = await status_mod.check_youtube_health(db=object(), community_id=42)

        assert health.state == "enabled"
        assert health.source == "api_key"
        assert seen_params["key"] == "env-key"

    async def test_no_community_id_skips_community_check_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `community_id` -- today's env-only callers get today's env-only behavior."""
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")

        async def boom_get_decrypted_tokens(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("community check must not run without a community_id")

        monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", boom_get_decrypted_tokens)
        _use_transport(
            monkeypatch, lambda _r: httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})
        )

        health = await status_mod.check_youtube_health()

        assert health.source == "api_key"

    async def test_cache_is_keyed_per_community_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A probe for one community must never leak into another's cached result.

        Nor into the env-only `None` slot's -- each gets its own network call.
        """
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")

        async def fake_get_decrypted_tokens(
            _db: Any, community_id: int, _provider: str, *, tenant_id: Any
        ) -> Any:
            if community_id == 1:
                return DecryptedTokens(
                    access_token="community-1-token", refresh_token=None, expires_at=None, scopes=[]
                )
            return None

        monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", fake_get_decrypted_tokens)

        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [{"id": "dQw4w9WgXcQ"}]})

        _use_transport(monkeypatch, handler)

        health_env_only = await status_mod.check_youtube_health()
        health_community_1 = await status_mod.check_youtube_health(db=object(), community_id=1)
        health_community_2 = await status_mod.check_youtube_health(db=object(), community_id=2)

        assert call_count == 3
        assert health_env_only.source == "api_key"
        assert health_community_1.source == "community"
        assert health_community_2.source == "api_key"


class TestSpotifyHealthStillWorks:
    """Sanity coverage for the pre-existing Spotify probe -- previously untested."""

    async def test_no_credentials_returns_unhealthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call should happen with no credentials")

        _use_transport(monkeypatch, boom)

        health = await status_mod.check_spotify_health()

        assert health.healthy is False
        assert health.cause == "spotify credentials not configured"

    async def test_healthy_token_response_returns_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
        _use_transport(monkeypatch, lambda _r: httpx.Response(200, json={"access_token": "tok"}))

        health = await status_mod.check_spotify_health()

        assert health.healthy is True
        assert health.cause is None

    async def test_401_returns_specific_cause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
        _use_transport(monkeypatch, lambda _r: httpx.Response(401, json={}))

        health = await status_mod.check_spotify_health()

        assert health.healthy is False
        assert health.cause == "spotify oauth token didn't work (401)"
