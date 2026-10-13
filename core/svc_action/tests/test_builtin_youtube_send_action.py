"""youtube_send_action.py -- real YouTube `liveChatMessages.insert` call, SSRF-guarded.

Same testing shape as `test_builtin_discord_send_action.py`/
`test_builtin_slack_send_action.py`: `httpx.MockTransport` stands in for
both the real YouTube Data API v3 host (`api_base`, overridden to a
literal IP -- no real DNS in unit tests) and Google's real OAuth hosts
(`oauth2.googleapis.com`, left as the real hostname since
`services/youtube_oauth.py` never routes those two fixed URLs through the
SSRF guard -- see that module's own docstring).
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers import youtube_send_action
from builtin_handlers.youtube_send_action import send_message
from services import youtube_oauth as oauth_mod

_TOKEN_PAYLOAD = {"access_token": "test-access-token", "expires_in": 3600, "token_type": "Bearer"}
_TOKENINFO_WITH_SCOPE = {"scope": f"{oauth_mod.YOUTUBE_FORCE_SSL_SCOPE} openid"}


@pytest.fixture(autouse=True)
def _reset_caches() -> Any:
    """Every cache touched by this bundle is module-level state -- isolate every test."""
    oauth_mod._token_cache.clear()
    oauth_mod._scope_cache.clear()
    youtube_send_action._video_live_chat_id_cache.clear()
    yield
    oauth_mod._token_cache.clear()
    oauth_mod._scope_cache.clear()
    youtube_send_action._video_live_chat_id_cache.clear()


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "test-refresh-token")


def _envelope(payload: dict | None = None) -> StageEnvelope:
    default_payload = {"text": "hello from waddlebot", "live_chat_id": "chat123"}
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.youtube.default",
        stage="action",
        event=PlatformEvent(
            platform="youtube",
            event_type="message",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-09-11T12:00:00Z",
        ),
        ts="2026-09-11T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base: dict[str, object] = {"api_base": "https://8.8.8.8/youtube/v3"}  # literal IP, no real DNS
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _routed_handler(*, data_api_response, token_response=None, tokeninfo_response=None):  # noqa: ANN001
    """Route by host: `oauth2.googleapis.com` (token/tokeninfo) vs the data-api host."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            if request.url.path == "/token":
                return (token_response or (lambda r: httpx.Response(200, json=_TOKEN_PAYLOAD)))(
                    request
                )
            return (
                tokeninfo_response or (lambda r: httpx.Response(200, json=_TOKENINFO_WITH_SCOPE))
            )(request)
        return data_api_response(request)

    return handler


class TestLiveChatIdResolution:
    """`live_chat_id` from payload is primary; `video_id` -> `videos.list` is the fallback."""

    async def test_payload_live_chat_id_used_directly_no_videos_list_call(self) -> None:
        captured = {}

        def data_api(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            if request.url.path.endswith("/videos"):
                raise AssertionError("videos.list should not be called when live_chat_id present")
            return httpx.Response(200, json={"id": "msg1"})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert "live_chat=chat123" in result.detail

    async def test_video_id_fallback_resolves_via_videos_list_and_is_cached(self) -> None:
        videos_list_calls = 0

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal videos_list_calls
            if request.url.path.endswith("/videos"):
                videos_list_calls += 1
                assert request.url.params["id"] == "vid42"
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"liveStreamingDetails": {"activeLiveChatId": "resolved-chat"}}
                        ]
                    },
                )
            return httpx.Response(200, json={"id": "msg1"})

        payload = {"text": "hi", "video_id": "vid42"}
        async with _client(_routed_handler(data_api_response=data_api)) as client:
            result1 = await send_message(_envelope(payload=payload), _config(), http_client=client)
            result2 = await send_message(_envelope(payload=payload), _config(), http_client=client)

        assert "live_chat=resolved-chat" in result1.detail
        assert "live_chat=resolved-chat" in result2.detail
        assert videos_list_calls == 1  # cached after the first resolution

    async def test_missing_live_chat_id_and_video_id_is_non_retryable(self) -> None:
        handler = _routed_handler(data_api_response=lambda r: httpx.Response(200))
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="live_chat_id.*video_id"):
                await send_message(_envelope(payload={"text": "hi"}), _config(), http_client=client)

    async def test_video_with_no_active_live_chat_is_non_retryable(self) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/videos"):
                return httpx.Response(
                    200, json={"items": [{"liveStreamingDetails": {}}]}
                )
            return httpx.Response(200, json={"id": "msg1"})

        payload = {"text": "hi", "video_id": "vid42"}
        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(NonRetryableTransportError, match="no active live chat"):
                await send_message(_envelope(payload=payload), _config(), http_client=client)

    async def test_video_not_found_is_non_retryable(self) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/videos"):
                return httpx.Response(200, json={"items": []})
            return httpx.Response(200, json={"id": "msg1"})

        payload = {"text": "hi", "video_id": "vid42"}
        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(NonRetryableTransportError, match="not found"):
                await send_message(_envelope(payload=payload), _config(), http_client=client)


async def test_missing_payload_text_is_non_retryable() -> None:
    async with _client(_routed_handler(data_api_response=lambda r: httpx.Response(200))) as client:
        with pytest.raises(NonRetryableTransportError, match="'text'"):
            await send_message(
                _envelope(payload={"live_chat_id": "chat123"}), _config(), http_client=client
            )


class TestSecretResolution:
    """Config `*_ref` keys resolve via `resolve_secret`; missing refs are a specific error."""

    async def test_missing_refresh_token_env_var_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
        handler = _routed_handler(data_api_response=lambda r: httpx.Response(200))
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="secret resolution failed"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_default_refs_used_when_config_omits_them(self) -> None:
        """No `client_id_ref`/`client_secret_ref`/`refresh_token_ref` in config -> env defaults."""
        captured = {}

        def token_response(request: httpx.Request) -> httpx.Response:
            body = httpx.QueryParams(request.content.decode())
            captured["client_id"] = body["client_id"]
            captured["client_secret"] = body["client_secret"]
            captured["refresh_token"] = body["refresh_token"]
            return httpx.Response(200, json=_TOKEN_PAYLOAD)

        handler = _routed_handler(
            data_api_response=lambda r: httpx.Response(200, json={"id": "msg1"}),
            token_response=token_response,
        )
        async with _client(handler) as client:
            await send_message(_envelope(), _config(), http_client=client)

        assert captured["client_id"] == "test-client-id"
        assert captured["client_secret"] == "test-client-secret"
        assert captured["refresh_token"] == "test-refresh-token"

    async def test_custom_refs_resolved_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CUSTOM_YT_REFRESH", "custom-refresh-token")
        captured = {}

        def token_response(request: httpx.Request) -> httpx.Response:
            body = httpx.QueryParams(request.content.decode())
            captured["refresh_token"] = body["refresh_token"]
            return httpx.Response(200, json=_TOKEN_PAYLOAD)

        handler = _routed_handler(
            data_api_response=lambda r: httpx.Response(200, json={"id": "msg1"}),
            token_response=token_response,
        )
        async with _client(handler) as client:
            await send_message(
                _envelope(), _config(refresh_token_ref="CUSTOM_YT_REFRESH"), http_client=client
            )

        assert captured["refresh_token"] == "custom-refresh-token"

    async def test_oauth_refresh_failure_is_non_retryable(self) -> None:
        handler = _routed_handler(
            data_api_response=lambda r: httpx.Response(200),
            token_response=lambda r: httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Token has been expired"}
            ),
        )
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="youtube oauth refresh failed"):
                await send_message(_envelope(), _config(), http_client=client)


class TestScopeCheck:
    """Pre-flight `token_has_scope` check runs before the send attempt."""

    async def test_missing_scope_blocks_send_before_any_data_api_call(self) -> None:
        data_api_called = False

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal data_api_called
            data_api_called = True
            return httpx.Response(200, json={"id": "msg1"})

        handler = _routed_handler(
            data_api_response=data_api,
            tokeninfo_response=lambda r: httpx.Response(200, json={"scope": "openid email"}),
        )
        async with _client(handler) as client:
            with pytest.raises(
                NonRetryableTransportError, match="lacks the youtube.force-ssl scope"
            ):
                await send_message(_envelope(), _config(), http_client=client)
        assert data_api_called is False

    async def test_scope_lookup_failure_does_not_block_send(self) -> None:
        """A `tokeninfo` infra hiccup never blocks the send -- the real API call decides."""
        handler = _routed_handler(
            data_api_response=lambda r: httpx.Response(200, json={"id": "msg1"}),
            tokeninfo_response=lambda r: httpx.Response(500),
        )
        async with _client(handler) as client:
            result = await send_message(_envelope(), _config(), http_client=client)
        assert result.transport == "bundle"


async def test_sends_real_live_chat_message_insert_request() -> None:
    """Fail-first verification: the bundle builds the real `liveChatMessages.insert` call."""
    captured = {}

    def data_api(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth_header"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "msg-999"})

    async with _client(_routed_handler(data_api_response=data_api)) as client:
        result = await send_message(_envelope(), _config(), http_client=client)

    assert captured["url"] == "https://8.8.8.8/youtube/v3/liveChatMessages?part=snippet"
    assert captured["auth_header"] == "Bearer test-access-token"
    assert _json.loads(captured["body"]) == {
        "snippet": {
            "liveChatId": "chat123",
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": "hello from waddlebot"},
        }
    }
    assert result.transport == "bundle"
    assert result.http_status == 200
    assert "live_chat=chat123" in result.detail
    # The access token itself must never leak into the audit-visible result detail.
    assert "test-access-token" not in result.detail


async def test_truncates_text_over_200_chars() -> None:
    long_text = "a" * 250
    captured = {}

    def data_api(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "msg1"})

    payload = {"text": long_text, "live_chat_id": "chat123"}
    async with _client(_routed_handler(data_api_response=data_api)) as client:
        await send_message(_envelope(payload=payload), _config(), http_client=client)

    sent_text = _json.loads(captured["body"])["snippet"]["textMessageDetails"]["messageText"]
    assert len(sent_text) == 200
    assert sent_text.endswith("…")
    assert sent_text[:199] == "a" * 199


async def test_text_at_exactly_200_chars_is_not_truncated() -> None:
    exact_text = "a" * 200
    captured = {}

    def data_api(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "msg1"})

    payload = {"text": exact_text, "live_chat_id": "chat123"}
    async with _client(_routed_handler(data_api_response=data_api)) as client:
        await send_message(_envelope(payload=payload), _config(), http_client=client)

    sent_text = _json.loads(captured["body"])["snippet"]["textMessageDetails"]["messageText"]
    assert sent_text == exact_text


class TestErrorMapping:
    """Every documented YouTube Data API v3 error response maps to its specific message."""

    async def test_persistent_401_after_one_forced_refresh_is_non_retryable(self) -> None:
        token_calls = 0

        def token_response(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            token_calls += 1
            return httpx.Response(200, json=_TOKEN_PAYLOAD)

        data_api_calls = 0

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal data_api_calls
            data_api_calls += 1
            return httpx.Response(401, json={"error": {"message": "invalid credentials"}})

        handler = _routed_handler(data_api_response=data_api, token_response=token_response)
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match=r"didn't work \(401\)"):
                await send_message(_envelope(), _config(), http_client=client)

        assert data_api_calls == 2  # original + the one forced-refresh retry
        assert token_calls == 2  # initial mint + the forced refresh

    async def test_401_then_success_on_retry(self) -> None:
        data_api_calls = 0

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal data_api_calls
            data_api_calls += 1
            if data_api_calls == 1:
                return httpx.Response(401, json={"error": {"message": "invalid credentials"}})
            return httpx.Response(200, json={"id": "msg1"})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert result.http_status == 200
        assert data_api_calls == 2

    @pytest.mark.parametrize("reason", ["insufficientPermissions", "forbidden"])
    async def test_403_scope_reasons_map_to_scope_message(self, reason: str) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"errors": [{"reason": reason}]}})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(
                NonRetryableTransportError, match="lacks the youtube.force-ssl scope"
            ):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_403_quota_exceeded_maps_to_quota_message(self) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(NonRetryableTransportError, match="quota exceeded"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_403_other_reason_is_generic_client_error(self) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "somethingElse"}]}})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(NonRetryableTransportError, match="client error"):
                await send_message(_envelope(), _config(), http_client=client)

    @pytest.mark.parametrize("reason", ["liveChatNotFound", "liveChatEnded"])
    async def test_404_reasons_map_to_chat_ended_message(self, reason: str) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"errors": [{"reason": reason}]}})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(NonRetryableTransportError, match="live chat has ended"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_429_retries_once_then_raises_rate_limited(self) -> None:
        calls = 0

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429)

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(RetryableTransportError, match="rate limited"):
                await send_message(_envelope(), _config(), http_client=client)

        assert calls == 2  # one immediate retry, never more

    async def test_429_then_success_on_retry(self) -> None:
        calls = 0

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"id": "msg1"})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert result.http_status == 200

    async def test_5xx_is_retryable(self) -> None:
        handler = _routed_handler(data_api_response=lambda r: httpx.Response(503))
        async with _client(handler) as client:
            with pytest.raises(RetryableTransportError, match="server error"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_network_error_is_retryable(self) -> None:
        def data_api(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            with pytest.raises(RetryableTransportError, match="request failed"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_private_host_api_base_is_blocked_non_retryable(self) -> None:
        """SSRF guard applies to the YouTube API base exactly like every other transport."""
        called = False

        def data_api(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={"id": "msg1"})

        handler = _routed_handler(data_api_response=data_api)
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="SSRF"):
                await send_message(
                    _envelope(),
                    _config(api_base="http://169.254.169.254"),
                    http_client=client,
                )
        assert called is False


class TestCommunityAwareOAuth:
    """gh-320: `get_access_token_for_community` wiring -- community id resolution + token source."""

    async def test_community_token_used_no_refresh_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refresh_called = False

        async def fake_resolve(community_id: int | None, provider: str) -> Any:
            assert community_id == 42
            assert provider == "youtube"
            return SimpleNamespace(access_token="community-access-token", source="community")

        monkeypatch.setattr(oauth_mod, "resolve_community_tokens", fake_resolve)

        def token_response(request: httpx.Request) -> httpx.Response:
            nonlocal refresh_called
            refresh_called = True
            return httpx.Response(200, json=_TOKEN_PAYLOAD)

        def data_api(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer community-access-token"
            return httpx.Response(200, json={"id": "msg1"})

        handler = _routed_handler(data_api_response=data_api, token_response=token_response)
        async with _client(handler) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert refresh_called is False
        assert result.http_status == 200

    async def test_no_community_connection_falls_back_to_env_refresh_flow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(community_id: int | None, provider: str) -> None:
            return None

        monkeypatch.setattr(oauth_mod, "resolve_community_tokens", fake_resolve)

        def data_api(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-access-token"
            return httpx.Response(200, json={"id": "msg1"})

        async with _client(_routed_handler(data_api_response=data_api)) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert result.http_status == 200

    async def test_malformed_community_id_falls_back_to_env_refresh_flow(self) -> None:
        """`envelope.community` not an integer -- `community_id` becomes `None`, never raises."""
        envelope = StageEnvelope(
            tenant="1",
            community="not-an-int",
            app_id="waddles.bot.youtube.default",
            stage="action",
            event=PlatformEvent(
                platform="youtube",
                event_type="message",
                actor=None,
                payload={"text": "hi", "live_chat_id": "chat123"},
                occurred_at="2026-09-11T12:00:00Z",
            ),
            ts="2026-09-11T12:00:00Z",
        )

        handler = _routed_handler(data_api_response=lambda r: httpx.Response(200))
        async with _client(handler) as client:
            result = await send_message(envelope, _config(), http_client=client)

        assert result.http_status == 200

    async def test_no_community_still_resolves_via_env_refresh_flow(self) -> None:
        """`envelope.community is None` (tenant-wide activation) -- `community_id` stays `None`."""
        envelope = StageEnvelope(
            tenant="1",
            community=None,
            app_id="waddles.bot.youtube.default",
            stage="action",
            event=PlatformEvent(
                platform="youtube",
                event_type="message",
                actor=None,
                payload={"text": "hi", "live_chat_id": "chat123"},
                occurred_at="2026-09-11T12:00:00Z",
            ),
            ts="2026-09-11T12:00:00Z",
        )

        handler = _routed_handler(data_api_response=lambda r: httpx.Response(200))
        async with _client(handler) as client:
            result = await send_message(envelope, _config(), http_client=client)

        assert result.http_status == 200
