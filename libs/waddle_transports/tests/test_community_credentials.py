"""community_credentials.py -- env fallback, community fetch, cache, invalidate."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from waddle_transports import community_credentials
from waddle_transports.community_credentials import (
    CommunityTokens,
    env_tokens,
    fetch_community_tokens,
    invalidate,
    resolve_community_tokens,
)


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    community_credentials.reset_cache_for_tests()
    yield
    community_credentials.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _sa_token(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the bootstrap at a projected-SA-token stand-in file; no static key anywhere."""
    sa = tmp_path / "sa-token"
    sa.write_text("projected-sa-token\n", encoding="utf-8")
    monkeypatch.setenv("SERVICE_JWT_SA_TOKEN_PATH", str(sa))
    monkeypatch.delenv("SERVICE_JWT_TOKEN_ENDPOINT", raising=False)
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    return sa


_RealAsyncClient = httpx.AsyncClient
_MINT_JWT = "minted.community.jwt"


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Monkeypatch `httpx.AsyncClient` so `fetch_community_tokens`'s own construction is mocked."""

    def _with_mint(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/service-token":
            return httpx.Response(200, json={"token": _MINT_JWT, "expires_in": 300})
        return handler(request)

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=httpx.MockTransport(_with_mint))

    monkeypatch.setattr(community_credentials.httpx, "AsyncClient", _factory)


# --- env_tokens ----------------------------------------------------------------


class TestEnvTokens:
    def test_hit_returns_env_source_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "rt-123")
        result = env_tokens("youtube")
        assert result == CommunityTokens(
            access_token=None, refresh_token="rt-123", expires_at=None, scopes=[], source="env"
        )

    def test_unset_env_var_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SPOTIFY_REFRESH_TOKEN", raising=False)
        assert env_tokens("spotify") is None

    def test_blank_env_var_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "")
        assert env_tokens("slack") is None

    def test_unknown_provider_returns_none(self) -> None:
        assert env_tokens("myspace") is None

    @pytest.mark.parametrize(
        ("provider", "env_var"),
        [
            ("youtube", "YOUTUBE_REFRESH_TOKEN"),
            ("spotify", "SPOTIFY_REFRESH_TOKEN"),
            ("twitch", "TWITCH_OAUTH_TOKEN"),
            ("discord", "DISCORD_BOT_TOKEN"),
            ("kick", "KICK_ACCESS_TOKEN"),
            ("slack", "SLACK_BOT_TOKEN"),
        ],
    )
    def test_every_provider_resolves_its_own_env_var(
        self, monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
    ) -> None:
        monkeypatch.setenv(env_var, "tok")
        result = env_tokens(provider)
        assert result is not None
        assert result.refresh_token == "tok"
        assert result.source == "env"


# --- fetch_community_tokens -----------------------------------------------------


class TestFetchCommunityTokens:
    async def test_full_response_maps_every_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/internal/communities/42/connections/youtube/token"
            return httpx.Response(
                200,
                json={
                    "access_token": "at-abc",
                    "refresh_token": "rt-abc",
                    "expires_at": "2026-09-11T12:00:00Z",
                    "scopes": ["youtube.readonly", "youtube.force-ssl"],
                },
            )

        _patch_client(monkeypatch, handler)
        result = await fetch_community_tokens(42, "youtube", hub_api_url="https://hub.internal")

        assert result == CommunityTokens(
            access_token="at-abc",
            refresh_token="rt-abc",
            expires_at=result.expires_at,  # checked precisely below
            scopes=["youtube.readonly", "youtube.force-ssl"],
            source="community",
        )
        assert result.expires_at is not None
        assert result.expires_at.isoformat() == "2026-09-11T12:00:00+00:00"

    async def test_hub_api_envelope_shape_is_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"access_token": "at-env", "scopes": ["s"]},
                    "meta": {"version": 1},
                },
            ),
        )
        result = await fetch_community_tokens(3, "youtube", hub_api_url="https://hub.internal")
        assert result is not None and result.access_token == "at-env"
        assert result.scopes == ["s"]

    async def test_minimal_response_defaults_optional_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": "at-only", "refresh_token": None, "expires_at": None}
            )

        _patch_client(monkeypatch, handler)
        result = await fetch_community_tokens(7, "kick", hub_api_url="https://hub.internal")

        assert result == CommunityTokens(
            access_token="at-only",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            source="community",
        )

    async def test_404_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(404))
        result = await fetch_community_tokens(1, "twitch", hub_api_url="https://hub.internal")
        assert result is None

    async def test_502_refresh_failed_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(502, json={"error": "refresh_failed"}))
        with caplog.at_level("WARNING", logger="waddle_transports.community_credentials"):
            result = await fetch_community_tokens(1, "twitch", hub_api_url="https://hub.internal")
        assert result is None
        assert any("fetch_rejected" in r.message and "502" in r.message for r in caplog.records)

    async def test_503_feature_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(503))
        result = await fetch_community_tokens(1, "slack", hub_api_url="https://hub.internal")
        assert result is None

    async def test_network_error_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _patch_client(monkeypatch, handler)
        with caplog.at_level("WARNING", logger="waddle_transports.community_credentials"):
            result = await fetch_community_tokens(1, "discord", hub_api_url="https://hub.internal")
        assert result is None
        assert any("fetch_unreachable" in r.message for r in caplog.records)

    async def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        _patch_client(monkeypatch, handler)
        result = await fetch_community_tokens(1, "discord", hub_api_url="https://hub.internal")
        assert result is None

    async def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _patch_client(monkeypatch, handler)
        result = await fetch_community_tokens(1, "youtube", hub_api_url="https://hub.internal")
        assert result is None

    async def test_non_dict_json_body_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=["nope"]))
        result = await fetch_community_tokens(1, "youtube", hub_api_url="https://hub.internal")
        assert result is None

    async def test_missing_access_token_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json={"refresh_token": "rt"}))
        result = await fetch_community_tokens(1, "youtube", hub_api_url="https://hub.internal")
        assert result is None

    async def test_malformed_expires_at_degrades_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(
            monkeypatch,
            lambda r: httpx.Response(200, json={"access_token": "at", "expires_at": "not-a-date"}),
        )
        result = await fetch_community_tokens(1, "youtube", hub_api_url="https://hub.internal")
        assert result is not None
        assert result.expires_at is None

    async def test_sends_minted_bearer_not_service_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERVICE_API_KEY", "svc-secret-xyz")  # must be ignored entirely
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"access_token": "at"})

        _patch_client(monkeypatch, handler)
        await fetch_community_tokens(9, "youtube", hub_api_url="https://hub.internal")
        assert len(seen) == 1
        assert seen[0].headers["Authorization"] == f"Bearer {_MINT_JWT}"
        assert "X-Service-Key" not in seen[0].headers

    async def test_mint_request_is_community_bound_with_sa_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mints: list[httpx.Request] = []

        def _factory(**kwargs: Any) -> httpx.AsyncClient:
            def h(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/internal/service-token":
                    mints.append(request)
                    return httpx.Response(200, json={"token": _MINT_JWT, "expires_in": 300})
                return httpx.Response(200, json={"access_token": "at"})

            return _RealAsyncClient(transport=httpx.MockTransport(h))

        monkeypatch.setattr(community_credentials.httpx, "AsyncClient", _factory)
        await fetch_community_tokens(42, "youtube", hub_api_url="https://hub.internal")
        await fetch_community_tokens(42, "youtube", hub_api_url="https://hub.internal")
        assert len(mints) == 1  # second call reuses the cached community JWT
        assert mints[0].headers["Authorization"] == "Bearer projected-sa-token"
        assert json.loads(mints[0].content) == {"scope": "connections:resolve", "community_id": 42}

    async def test_missing_sa_token_file_returns_none_and_never_calls_resource(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("SERVICE_JWT_SA_TOKEN_PATH", str(tmp_path / "absent"))
        seen: list[httpx.Request] = []
        _patch_client(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json={}))
        with caplog.at_level("WARNING", logger="waddle_transports.community_credentials"):
            result = await fetch_community_tokens(1, "youtube", hub_api_url="https://hub.internal")
        assert result is None
        assert seen == []
        assert any("mint_no_sa_token" in r.message for r in caplog.records)

    async def test_empty_sa_token_file_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, _sa_token: Any
    ) -> None:
        _sa_token.write_text("  \n", encoding="utf-8")
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "at"}))
        assert await fetch_community_tokens(1, "youtube", hub_api_url="https://h.internal") is None

    @pytest.mark.parametrize(
        "mint_response",
        [
            httpx.Response(401, json={"error": "unauthorized"}),
            httpx.Response(200, content=b"nope"),
            httpx.Response(200, json={"token": "t"}),
            httpx.Response(200, json={"token": "", "expires_in": 300}),
            httpx.Response(200, json={"token": "t", "expires_in": 0}),
        ],
    )
    async def test_bad_mint_response_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, mint_response: httpx.Response
    ) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/internal/service-token":
                return mint_response
            raise AssertionError("resource must not be called without a minted JWT")

        monkeypatch.setattr(
            community_credentials.httpx,
            "AsyncClient",
            lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(h)),
        )
        assert await fetch_community_tokens(1, "youtube", hub_api_url="https://h.internal") is None

    async def test_rejected_jwt_is_evicted_from_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(401))
        await fetch_community_tokens(5, "youtube", hub_api_url="https://hub.internal")
        assert 5 not in community_credentials._jwt_cache

    async def test_mint_endpoint_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "SERVICE_JWT_TOKEN_ENDPOINT", "https://issuer.internal/internal/service-token"
        )
        hosts: list[str] = []

        def h(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            if request.url.path == "/internal/service-token":
                return httpx.Response(200, json={"token": _MINT_JWT, "expires_in": 300})
            return httpx.Response(200, json={"access_token": "at"})

        monkeypatch.setattr(
            community_credentials.httpx,
            "AsyncClient",
            lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(h)),
        )
        await fetch_community_tokens(1, "youtube", hub_api_url="https://hub.internal")
        assert hosts == ["issuer.internal", "hub.internal"]

    async def test_uses_explicit_hub_api_url_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HUB_API_INTERNAL_URL", "https://env-should-not-win.internal")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["host"] = request.url.host
            return httpx.Response(200, json={"access_token": "at"})

        _patch_client(monkeypatch, handler)
        await fetch_community_tokens(1, "youtube", hub_api_url="https://explicit.internal")
        assert captured["host"] == "explicit.internal"

    async def test_falls_back_to_hub_api_internal_url_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HUB_API_INTERNAL_URL", "https://internal-env.example")
        monkeypatch.delenv("HUB_API_URL", raising=False)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["host"] = request.url.host
            return httpx.Response(200, json={"access_token": "at"})

        _patch_client(monkeypatch, handler)
        await fetch_community_tokens(1, "youtube")
        assert captured["host"] == "internal-env.example"

    async def test_falls_back_to_hub_api_url_env_when_internal_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HUB_API_INTERNAL_URL", raising=False)
        monkeypatch.setenv("HUB_API_URL", "https://public-env.example")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["host"] = request.url.host
            return httpx.Response(200, json={"access_token": "at"})

        _patch_client(monkeypatch, handler)
        await fetch_community_tokens(1, "youtube")
        assert captured["host"] == "public-env.example"

    async def test_falls_back_to_builtin_default_when_no_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HUB_API_INTERNAL_URL", raising=False)
        monkeypatch.delenv("HUB_API_URL", raising=False)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["host"] = request.url.host
            return httpx.Response(200, json={"access_token": "at"})

        _patch_client(monkeypatch, handler)
        await fetch_community_tokens(1, "youtube")
        assert captured["host"] == "hub-api"


# --- resolve_community_tokens ---------------------------------------------------


class TestResolveCommunityTokens:
    async def test_community_hit_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "should-not-be-used")

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> CommunityTokens:
            return CommunityTokens(
                access_token="community-at",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                source="community",
            )

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        result = await resolve_community_tokens(42, "youtube")
        assert result is not None
        assert result.access_token == "community-at"
        assert result.source == "community"

    async def test_falls_back_to_env_when_community_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "env-fallback-rt")

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        result = await resolve_community_tokens(42, "youtube")
        assert result == CommunityTokens(
            access_token=None,
            refresh_token="env-fallback-rt",
            expires_at=None,
            scopes=[],
            source="env",
        )

    async def test_none_community_id_skips_hub_api_call_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        async def _fake_fetch(*args: Any, **kwargs: Any) -> None:
            nonlocal called
            called = True
            return None

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "bot-tok")
        result = await resolve_community_tokens(None, "slack")
        assert called is False
        assert result is not None
        assert result.refresh_token == "bot-tok"

    async def test_no_community_and_no_env_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KICK_ACCESS_TOKEN", raising=False)
        result = await resolve_community_tokens(None, "kick")
        assert result is None

    async def test_cache_hit_avoids_second_fetch_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> CommunityTokens:
            nonlocal call_count
            call_count += 1
            return CommunityTokens(
                access_token="at",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                source="community",
            )

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        first = await resolve_community_tokens(1, "youtube")
        second = await resolve_community_tokens(1, "youtube")
        assert call_count == 1
        assert first == second

    async def test_positive_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = 0

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> CommunityTokens:
            nonlocal call_count
            call_count += 1
            return CommunityTokens(
                access_token=f"at-{call_count}",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                source="community",
            )

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)

        fake_now = 1_000.0
        monkeypatch.setattr(community_credentials.time, "monotonic", lambda: fake_now)
        first = await resolve_community_tokens(1, "youtube")

        fake_now += 61.0  # past the 60s positive TTL
        second = await resolve_community_tokens(1, "youtube")

        assert call_count == 2
        assert first is not None
        assert second is not None
        assert first.access_token != second.access_token

    async def test_negative_cache_expires_after_shorter_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            return None

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)

        fake_now = 2_000.0
        monkeypatch.setattr(community_credentials.time, "monotonic", lambda: fake_now)
        first = await resolve_community_tokens(1, "youtube")
        assert first is None
        assert call_count == 1

        fake_now += 16.0  # past the 15s negative TTL, before a 60s positive one would expire
        second = await resolve_community_tokens(1, "youtube")
        assert second is None
        assert call_count == 2

    async def test_negative_cache_still_fresh_within_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            return None

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)

        fake_now = 3_000.0
        monkeypatch.setattr(community_credentials.time, "monotonic", lambda: fake_now)
        await resolve_community_tokens(1, "youtube")

        fake_now += 5.0  # still within the 15s negative TTL
        await resolve_community_tokens(1, "youtube")
        assert call_count == 1

    async def test_invalidate_clears_only_that_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = 0

        async def _fake_fetch(community_id: int, provider: str, **kwargs: Any) -> CommunityTokens:
            nonlocal call_count
            call_count += 1
            return CommunityTokens(
                access_token=f"at-{call_count}",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                source="community",
            )

        monkeypatch.setattr(community_credentials, "fetch_community_tokens", _fake_fetch)
        first = await resolve_community_tokens(1, "youtube")
        second_same_key = await resolve_community_tokens(1, "youtube")
        assert first == second_same_key
        assert call_count == 1

        invalidate(1, "youtube")
        third = await resolve_community_tokens(1, "youtube")
        assert call_count == 2
        assert third is not None
        assert first is not None
        assert third.access_token != first.access_token

    async def test_invalidate_on_uncached_key_is_a_no_op(self) -> None:
        invalidate(999, "youtube")  # must not raise
