"""kick_send_action.py -- real Kick `messages/send/<chatroom_id>` call, SSRF-guarded.

Kick API + Kick's OAuth token endpoint are both mocked (`httpx.
MockTransport`, routed by host) -- a real send needs live credentials.
Mirrors `test_builtin_slack_send_action.py`/`test_builtin_youtube_send_
action.py`'s structure and conventions (chatroom_id resolution precedence,
SSRF guard, retryable vs non-retryable classification) plus Kick-specific
behavior: two credential modes (stored token vs client-credentials) and a
single retry-on-401 that re-fetches the access token.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.kick_send_action import send_message
from services import kick_oauth as oauth_mod

_TOKEN_PAYLOAD = {"access_token": "app-token-xyz", "expires_in": 3600, "token_type": "Bearer"}


@pytest.fixture(autouse=True)
def _reset_cache() -> Any:
    oauth_mod._token_cache.clear()
    yield
    oauth_mod._token_cache.clear()


def _envelope(payload: dict | None = None) -> StageEnvelope:
    default_payload = {"text": "hello from waddlebot", "chatroom_id": 999}
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.kick.default",
        stage="action",
        event=PlatformEvent(
            platform="kick",
            event_type="message",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-09-11T12:00:00Z",
        ),
        ts="2026-09-11T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base: dict[str, object] = {
        "access_token_ref": "TEST_KICK_ACCESS_TOKEN",
        "api_base": "https://8.8.8.8/api/v2",  # literal IP -- no real DNS in unit tests
    }
    base.update(overrides)
    return base


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _routed_handler(*, api_response: Any, token_response: Any = None) -> Any:
    """Route by host: `id.kick.com` (OAuth token) vs the Kick API host."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.kick.com":
            return (token_response or (lambda r: httpx.Response(200, json=_TOKEN_PAYLOAD)))(request)
        return api_response(request)

    return handler


class TestChatroomIdResolution:
    """Reply-in-place: payload.chatroom_id is primary, config.chatroom_id is a fallback only."""

    async def test_no_chatroom_id_from_either_source_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        handler = _routed_handler(api_response=lambda r: httpx.Response(200, json={"id": "m1"}))
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="chatroom_id"):
                await send_message(_envelope(payload={"text": "hi"}), _config(), http_client=client)

    async def test_payload_chatroom_id_takes_precedence_over_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        captured = {}

        def api(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"id": "m1"})

        payload = {"text": "hi", "chatroom_id": 111}
        async with _client(_routed_handler(api_response=api)) as client:
            await send_message(
                _envelope(payload=payload), _config(chatroom_id=222), http_client=client
            )
        assert captured["path"].endswith("/messages/send/111")

    async def test_config_chatroom_id_used_as_fallback_when_payload_lacks_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        captured = {}

        def api(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"id": "m1"})

        async with _client(_routed_handler(api_response=api)) as client:
            await send_message(
                _envelope(payload={"text": "hi"}), _config(chatroom_id=222), http_client=client
            )
        assert captured["path"].endswith("/messages/send/222")

    async def test_zero_chatroom_id_is_not_treated_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`0` is falsy but structurally valid -- the resolution guard must not drop it."""
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        captured = {}

        def api(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"id": "m1"})

        payload = {"text": "hi", "chatroom_id": 0}
        async with _client(_routed_handler(api_response=api)) as client:
            await send_message(_envelope(payload=payload), _config(), http_client=client)
        assert captured["path"].endswith("/messages/send/0")


async def test_missing_payload_text_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
    handler = _routed_handler(api_response=lambda r: httpx.Response(200, json={"id": "m1"}))
    async with _client(handler) as client:
        with pytest.raises(NonRetryableTransportError, match="'text'"):
            await send_message(
                _envelope(payload={"chatroom_id": 999}), _config(), http_client=client
            )


class TestCredentialModes:
    """Stored token (`access_token_ref`) takes precedence; client-credentials is the fallback."""

    async def test_stored_token_used_directly_no_oauth_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        captured = {}

        def api(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"id": "m1"})

        def token_response(request: httpx.Request) -> httpx.Response:
            raise AssertionError("client-credentials must not be attempted with a stored token")

        handler = _routed_handler(api_response=api, token_response=token_response)
        async with _client(handler) as client:
            await send_message(_envelope(), _config(), http_client=client)
        assert captured["auth"] == "Bearer tok-abc"

    async def test_falls_back_to_client_credentials_when_no_stored_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_KICK_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("KICK_CLIENT_ID", "cid")
        monkeypatch.setenv("KICK_CLIENT_SECRET", "csecret")
        captured = {}

        def api(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"id": "m1"})

        handler = _routed_handler(api_response=api)
        async with _client(handler) as client:
            await send_message(_envelope(), _config(), http_client=client)
        assert captured["auth"] == "Bearer app-token-xyz"

    async def test_no_usable_credential_mode_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_KICK_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("KICK_CLIENT_ID", raising=False)
        monkeypatch.delenv("KICK_CLIENT_SECRET", raising=False)
        handler = _routed_handler(api_response=lambda r: httpx.Response(200, json={"id": "m1"}))
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="stored access token or both"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_custom_client_id_secret_refs_resolved_from_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_KICK_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("CUSTOM_CID", "custom-cid")
        monkeypatch.setenv("CUSTOM_CSECRET", "custom-csecret")
        captured = {}

        def token_response(request: httpx.Request) -> httpx.Response:
            body = httpx.QueryParams(request.content.decode())
            captured["client_id"] = body["client_id"]
            captured["client_secret"] = body["client_secret"]
            return httpx.Response(200, json=_TOKEN_PAYLOAD)

        handler = _routed_handler(
            api_response=lambda r: httpx.Response(200, json={"id": "m1"}),
            token_response=token_response,
        )
        async with _client(handler) as client:
            await send_message(
                _envelope(),
                _config(client_id_ref="CUSTOM_CID", client_secret_ref="CUSTOM_CSECRET"),
                http_client=client,
            )
        assert captured["client_id"] == "custom-cid"
        assert captured["client_secret"] == "custom-csecret"


async def test_sends_real_message_send_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-first verification: the bundle builds the real `messages/send/<chatroom_id>` call."""
    monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
    captured = {}

    def api(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth_header"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "msg-999"})

    async with _client(_routed_handler(api_response=api)) as client:
        result = await send_message(_envelope(), _config(), http_client=client)

    assert captured["url"] == "https://8.8.8.8/api/v2/messages/send/999"
    assert captured["auth_header"] == "Bearer tok-abc"
    assert _json.loads(captured["body"]) == {"content": "hello from waddlebot", "type": "message"}
    assert result.transport == "bundle"
    assert result.http_status == 200
    assert "chatroom=999" in result.detail
    assert "tok-abc" not in result.detail


class TestErrorMapping:
    async def test_persistent_401_after_one_forced_refresh_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_KICK_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("KICK_CLIENT_ID", "cid")
        monkeypatch.setenv("KICK_CLIENT_SECRET", "csecret")
        token_calls = 0
        api_calls = 0

        def token_response(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            token_calls += 1
            return httpx.Response(200, json=_TOKEN_PAYLOAD)

        def api(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            api_calls += 1
            return httpx.Response(401, json={"error": "invalid credentials"})

        handler = _routed_handler(api_response=api, token_response=token_response)
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match=r"didn't work \(401\)"):
                await send_message(_envelope(), _config(), http_client=client)

        assert api_calls == 2  # original + the one forced-refresh retry
        assert token_calls == 2  # initial mint + the forced refresh

    async def test_401_then_success_on_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        api_calls = 0

        def api(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            api_calls += 1
            if api_calls == 1:
                return httpx.Response(401, json={"error": "invalid credentials"})
            return httpx.Response(200, json={"id": "m1"})

        async with _client(_routed_handler(api_response=api)) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert result.http_status == 200
        assert api_calls == 2

    async def test_403_is_forbidden_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        handler = _routed_handler(api_response=lambda r: httpx.Response(403))
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match=r"forbidden \(403\)"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_429_retries_once_then_raises_rate_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        calls = 0

        def api(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429)

        async with _client(_routed_handler(api_response=api)) as client:
            with pytest.raises(RetryableTransportError, match=r"rate limited \(429\)"):
                await send_message(_envelope(), _config(), http_client=client)
        assert calls == 2  # one immediate retry, never more

    async def test_429_then_success_on_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        calls = 0

        def api(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"id": "m1"})

        async with _client(_routed_handler(api_response=api)) as client:
            result = await send_message(_envelope(), _config(), http_client=client)
        assert result.http_status == 200

    async def test_5xx_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        handler = _routed_handler(api_response=lambda r: httpx.Response(503))
        async with _client(handler) as client:
            with pytest.raises(RetryableTransportError, match="kick API error"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_other_4xx_is_generic_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        handler = _routed_handler(api_response=lambda r: httpx.Response(400))
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match=r"kick API error: HTTP 400"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_network_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")

        def api(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with _client(_routed_handler(api_response=api)) as client:
            with pytest.raises(RetryableTransportError, match="request failed"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_private_host_api_base_is_blocked_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SSRF guard applies to the Kick API base exactly like every other transport."""
        monkeypatch.setenv("TEST_KICK_ACCESS_TOKEN", "tok-abc")
        called = False

        def api(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={"id": "m1"})

        handler = _routed_handler(api_response=api)
        async with _client(handler) as client:
            with pytest.raises(NonRetryableTransportError, match="SSRF"):
                await send_message(
                    _envelope(),
                    _config(api_base="http://169.254.169.254"),
                    http_client=client,
                )
        assert called is False
