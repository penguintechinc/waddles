"""Tests for `services/music_providers/youtube.py`'s community-connected credential mode.

Issue #320, chunk C8: a community's own connected YouTube account
(`services.community_connections.get_decrypted_tokens()`/
`store_refreshed_access_token()`) is preferred ahead of this module's
pre-existing env-only credential modes (covered by `test_music_providers.py`/
`test_music_providers_youtube_oauth.py`, both entirely unaffected -- they
never pass `db`/`community_id`, which preserves their env-only behavior
exactly). Split into its own file for the same reason
`test_music_providers_youtube_oauth.py` was split out of `test_music_
providers.py`: a third, independent credential path gets its own
self-contained fixtures rather than growing a sibling module further.

Same mocked-transport approach as the sibling test files: `httpx.
MockTransport` swapped in for `httpx.AsyncClient`, so request building/
header encoding all run for real -- no real socket ever touched.
`get_decrypted_tokens`/`store_refreshed_access_token`/`refresh_access_token`
are monkeypatched directly on `youtube_mod` (the names this module imports
them under), never a real DB or a real Google OAuth endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from services.community_connections import DecryptedTokens
from services.music_providers import youtube as youtube_mod
from services.oauth_providers import OAuthExchangeError, TokenResponse

_RealAsyncClient = httpx.AsyncClient

_YT_VIDEO_ITEM = {
    "id": "dQw4w9WgXcQ",
    "snippet": {
        "title": "Rick Astley - Never Gonna Give You Up",
        "channelTitle": "Rick Astley",
        "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/default.jpg"}},
    },
    "contentDetails": {"duration": "PT3M33S"},
}

_WATCH_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

_COMMUNITY_ID = 42


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
def _no_real_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test controls its own env credential source explicitly; no real `~/.youtube.token`."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(
        youtube_mod, "_TOKEN_FILE", youtube_mod._TOKEN_FILE.parent / "__nope__.token"
    )


@pytest.fixture(autouse=True)
def _reset_module_caches() -> Any:
    """OAuth-token + community-auth caches are module-level state -- isolate every test."""
    youtube_mod._oauth_token_cache = None
    youtube_mod._community_auth_cache = {}
    yield
    youtube_mod._oauth_token_cache = None
    youtube_mod._community_auth_cache = {}


def _fresh_tokens(
    *, access_token: str = "community-access-token", refresh_token: str | None = "community-refresh"
) -> DecryptedTokens:
    return DecryptedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["https://www.googleapis.com/auth/youtube.readonly"],
    )


def _expired_tokens(
    *, access_token: str = "stale-access-token", refresh_token: str | None = "community-refresh"
) -> DecryptedTokens:
    return DecryptedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        scopes=[],
    )


def _mock_get_decrypted_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tokens: DecryptedTokens | None,
    *,
    calls: list[int] | None = None,
) -> None:
    async def _fake(
        _db: Any, community_id: int, provider: str, *, tenant_id: Any
    ) -> DecryptedTokens | None:
        assert tenant_id is youtube_mod.SYSTEM  # no request tenant: explicit SYSTEM, never None
        assert community_id == _COMMUNITY_ID
        assert provider == "youtube"
        if calls is not None:
            calls.append(1)
        return tokens

    monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", _fake)


class TestCommunityTokenFresh:
    """A fresh (far-from-expiry) community token is used directly -- no refresh call."""

    async def test_fresh_token_used_as_bearer_no_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_get_decrypted_tokens(monkeypatch, _fresh_tokens(access_token="fresh-tok"))

        async def boom_refresh(*_args: Any, **_kwargs: Any) -> TokenResponse:
            raise AssertionError("a fresh token must never be refreshed")

        monkeypatch.setattr(youtube_mod, "refresh_access_token", boom_refresh)

        seen_auth_headers: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth_headers.append(request.headers.get("Authorization"))
            if "topicDetails" in (request.url.params.get("part") or ""):
                return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})
            return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})

        _use_transport(monkeypatch, handler)

        track = await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

        assert track.external_id == "dQw4w9WgXcQ"
        # Both the primary videos.list call and the label-enrichment call use the
        # same resolved (community) auth -- see module docstring "(and for
        # label/tag checks)" requirement.
        assert seen_auth_headers == ["Bearer fresh-tok", "Bearer fresh-tok"]

    async def test_no_expiry_at_all_treated_as_fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`expires_at=None` (never expires / unknown) -- used as-is, no refresh."""
        tokens = DecryptedTokens(
            access_token="fresh-tok", refresh_token="refresh", expires_at=None, scopes=[]
        )
        _mock_get_decrypted_tokens(monkeypatch, tokens)

        async def boom_refresh(*_args: Any, **_kwargs: Any) -> TokenResponse:
            raise AssertionError("expires_at=None must never trigger a refresh")

        monkeypatch.setattr(youtube_mod, "refresh_access_token", boom_refresh)
        _use_transport(
            monkeypatch, lambda _r: httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})
        )

        track = await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)
        assert track.external_id == "dQw4w9WgXcQ"

    async def test_cached_across_two_calls_single_db_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        _mock_get_decrypted_tokens(monkeypatch, _fresh_tokens(), calls=calls)
        _use_transport(
            monkeypatch, lambda _r: httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})
        )

        await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)
        await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

        assert len(calls) == 1


class TestCommunityTokenExpiredRefreshes:
    """An expiring/expired community token is refreshed and persisted before use."""

    async def test_expired_token_refreshes_and_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_get_decrypted_tokens(monkeypatch, _expired_tokens(refresh_token="the-refresh-token"))

        async def fake_refresh(provider: str, *, refresh_token: str) -> TokenResponse:
            assert provider == "youtube"
            assert refresh_token == "the-refresh-token"
            return TokenResponse(
                access_token="refreshed-tok",
                refresh_token=None,
                expires_in=3600,
                scopes=["https://www.googleapis.com/auth/youtube.readonly"],
                token_type="Bearer",
            )

        monkeypatch.setattr(youtube_mod, "refresh_access_token", fake_refresh)

        stored: dict[str, Any] = {}

        async def fake_store(
            _db: Any,
            community_id: int,
            provider: str,
            *,
            access_token: str,
            expires_at: Any,
            tenant_id: Any,
        ) -> None:
            assert tenant_id is youtube_mod.SYSTEM
            stored["community_id"] = community_id
            stored["provider"] = provider
            stored["access_token"] = access_token
            stored["expires_at"] = expires_at

        monkeypatch.setattr(youtube_mod, "store_refreshed_access_token", fake_store)

        seen_auth_header = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_auth_header
            seen_auth_header = request.headers.get("Authorization")
            return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})

        _use_transport(monkeypatch, handler)

        track = await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

        assert track.external_id == "dQw4w9WgXcQ"
        assert seen_auth_header == "Bearer refreshed-tok"
        assert stored["community_id"] == _COMMUNITY_ID
        assert stored["provider"] == "youtube"
        assert stored["access_token"] == "refreshed-tok"
        assert stored["expires_at"] is not None
        assert stored["expires_at"] > datetime.now(UTC)

    async def test_expiring_within_safety_margin_refreshes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not yet expired, but inside the 60s safety margin -- still refreshed proactively."""
        tokens = DecryptedTokens(
            access_token="about-to-expire",
            refresh_token="refresh-me",
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            scopes=[],
        )
        _mock_get_decrypted_tokens(monkeypatch, tokens)

        async def fake_refresh(_provider: str, *, refresh_token: str) -> TokenResponse:
            return TokenResponse(
                access_token="refreshed-tok",
                refresh_token=None,
                expires_in=3600,
                scopes=[],
                token_type="Bearer",
            )

        monkeypatch.setattr(youtube_mod, "refresh_access_token", fake_refresh)

        async def fake_store(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(youtube_mod, "store_refreshed_access_token", fake_store)

        seen_auth_header = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_auth_header
            seen_auth_header = request.headers.get("Authorization")
            return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})

        _use_transport(monkeypatch, handler)

        await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)
        assert seen_auth_header == "Bearer refreshed-tok"

    async def test_expired_no_refresh_token_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
        _mock_get_decrypted_tokens(monkeypatch, _expired_tokens(refresh_token=None))

        async def boom_refresh(*_args: Any, **_kwargs: Any) -> TokenResponse:
            raise AssertionError("no refresh token -- refresh_access_token must not be called")

        monkeypatch.setattr(youtube_mod, "refresh_access_token", boom_refresh)

        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})

        _use_transport(monkeypatch, handler)

        track = await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

        assert track.external_id == "dQw4w9WgXcQ"
        assert seen_params["key"] == "env-key"


class TestCommunityRefreshFailureFallsBackToEnv:
    """A failed refresh (`OAuthExchangeError`) falls through to env credentials.

    Logged at WARNING, never raised out of `resolve()`.
    """

    async def test_refresh_failure_falls_back_to_env_api_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
        _mock_get_decrypted_tokens(monkeypatch, _expired_tokens())

        async def fake_refresh(*_args: Any, **_kwargs: Any) -> TokenResponse:
            raise OAuthExchangeError("youtube: token endpoint returned HTTP 400")

        monkeypatch.setattr(youtube_mod, "refresh_access_token", fake_refresh)

        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})

        _use_transport(monkeypatch, handler)

        with caplog.at_level("WARNING"):
            track = await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

        assert track.external_id == "dQw4w9WgXcQ"
        assert seen_params["key"] == "env-key"
        assert any(
            "community_auth" in record.message and "refresh_failed" in record.message
            for record in caplog.records
        )
        # Never log token material.
        assert not any("community-refresh" in record.message for record in caplog.records)

    async def test_refresh_failure_with_no_env_raises_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.music_providers.errors import ProviderUnavailable

        _mock_get_decrypted_tokens(monkeypatch, _expired_tokens())

        async def fake_refresh(*_args: Any, **_kwargs: Any) -> TokenResponse:
            raise OAuthExchangeError("youtube: token endpoint returned HTTP 400")

        monkeypatch.setattr(youtube_mod, "refresh_access_token", fake_refresh)

        def boom(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call should happen with nothing usable")

        _use_transport(monkeypatch, boom)

        with pytest.raises(ProviderUnavailable):
            await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)


class TestNoCommunityConnectionFallsBackToEnv:
    """No community connection at all -- straight to this module's own env precedence."""

    async def test_no_connection_uses_env_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
        calls: list[int] = []
        _mock_get_decrypted_tokens(monkeypatch, None, calls=calls)

        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})

        _use_transport(monkeypatch, handler)

        track = await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

        assert track.external_id == "dQw4w9WgXcQ"
        assert seen_params["key"] == "env-key"
        assert len(calls) == 1

    async def test_no_connection_and_no_env_raises_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.music_providers.errors import ProviderUnavailable

        _mock_get_decrypted_tokens(monkeypatch, None)

        def boom(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call should happen with nothing configured")

        _use_transport(monkeypatch, boom)

        with pytest.raises(ProviderUnavailable):
            await youtube_mod.resolve(_WATCH_URL, db=object(), community_id=_COMMUNITY_ID)

    async def test_omitting_db_and_community_id_skips_community_check_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`db=None, community_id=None` (the default) -- pre-issue-#320 behavior, unchanged."""
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")

        async def boom_get_decrypted_tokens(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("community check must not run without db/community_id")

        monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", boom_get_decrypted_tokens)
        _use_transport(
            monkeypatch, lambda _r: httpx.Response(200, json={"items": [_YT_VIDEO_ITEM]})
        )

        track = await youtube_mod.resolve(_WATCH_URL)
        assert track.external_id == "dQw4w9WgXcQ"


class TestYoutubeCredentialsSource:
    """`youtube_credentials_source()` -- precedence + presence-only reporting."""

    async def test_community_connected_reports_community(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
        _mock_get_decrypted_tokens(monkeypatch, _fresh_tokens())

        source = await youtube_mod.youtube_credentials_source(object(), _COMMUNITY_ID)
        assert source == "community"

    async def test_no_community_id_reports_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("community_id=None must skip the community check")

        monkeypatch.setattr(youtube_mod, "get_decrypted_tokens", boom)

        source = await youtube_mod.youtube_credentials_source(object(), None)
        assert source == "api_key"

    async def test_no_connection_api_key_set_reports_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
        _mock_get_decrypted_tokens(monkeypatch, None)

        source = await youtube_mod.youtube_credentials_source(object(), _COMMUNITY_ID)
        assert source == "api_key"

    async def test_no_connection_oauth_trio_set_reports_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "rtoken")
        _mock_get_decrypted_tokens(monkeypatch, None)

        source = await youtube_mod.youtube_credentials_source(object(), _COMMUNITY_ID)
        assert source == "env"

    async def test_nothing_configured_reports_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_get_decrypted_tokens(monkeypatch, None)

        source = await youtube_mod.youtube_credentials_source(object(), _COMMUNITY_ID)
        assert source == "none"

    async def test_api_key_wins_over_env_oauth_trio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "rtoken")
        _mock_get_decrypted_tokens(monkeypatch, None)

        source = await youtube_mod.youtube_credentials_source(object(), _COMMUNITY_ID)
        assert source == "api_key"
