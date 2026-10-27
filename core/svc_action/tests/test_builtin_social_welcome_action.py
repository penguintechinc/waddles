"""Tests for social_welcome_action handler."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.social_welcome_action import send_welcome


def _envelope(payload: dict | None = None) -> StageEnvelope:
    """Create a test StageEnvelope for a welcome message."""
    default_payload = {"text": "Welcome!", "channel_id": "chan-123", "author_id": "user-1"}
    return StageEnvelope(
        tenant="tenant-1",
        community="community-1",
        app_id="waddles.social.welcome.default",
        stage="action",
        event=PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-01-01T00:00:00Z",
        ),
        ts="2026-01-01T00:00:00Z",
    )


def _config(**overrides: object) -> dict:
    """Create a test config with common defaults."""
    base = {
        "api_token_ref": "DISCORD_BOT_TOKEN",
        "api_base": "https://discord.com/api/v10",
    }
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    """Create an AsyncClient with a mock transport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class TestSendWelcome:
    """Tests for send_welcome entrypoint."""

    async def test_missing_channel_id_is_non_retryable(self) -> None:
        """Missing channel_id raises NonRetryableTransportError."""
        envelope = _envelope(payload={"text": "Welcome!"})
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="channel_id"):
                await send_welcome(envelope, _config(), http_client=client)

    async def test_config_channel_id_fallback(self) -> None:
        """Falls back to config['channel_id'] if event payload has none."""
        envelope = _envelope(payload={"text": "Welcome!"})
        config = _config(channel_id="config-chan-42")

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "msg-1"})

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                result = await send_welcome(envelope, config, http_client=client)
                assert "config-chan-42" in captured.get("url", "")
                assert result.transport == "bundle"

    async def test_event_channel_id_primary(self) -> None:
        """Event channel_id takes priority over config."""
        envelope = _envelope(payload={"text": "Welcome!", "channel_id": "event-chan-1"})
        config = _config(channel_id="config-chan-42")

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "msg-1"})

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                await send_welcome(envelope, config, http_client=client)
                assert "event-chan-1" in captured.get("url", "")

    async def test_missing_text_is_non_retryable(self) -> None:
        """Missing 'text' in event.payload raises NonRetryableTransportError."""
        envelope = _envelope(payload={"channel_id": "chan-1"})
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="text"):
                await send_welcome(envelope, _config(), http_client=client)

    async def test_missing_token_ref_is_non_retryable(self) -> None:
        """Missing api_token_ref raises NonRetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="api_token_ref"):
                await send_welcome(envelope, _config(api_token_ref=""), http_client=client)

    async def test_empty_token_ref_is_non_retryable(self) -> None:
        """Empty api_token_ref raises NonRetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="api_token_ref"):
                config = _config()
                config["api_token_ref"] = ""
                await send_welcome(envelope, config, http_client=client)

    async def test_secret_resolution_failure_is_non_retryable(self) -> None:
        """Token resolution failure raises NonRetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(200)) as client:
            with patch("builtin_handlers.social_welcome_action.resolve_secret") as mock_resolve:
                from waddle_transports.signing import SecretResolutionError

                mock_resolve.side_effect = SecretResolutionError("token not found")
                with pytest.raises(NonRetryableTransportError, match="token resolution failed"):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_http_timeout_is_retryable(self) -> None:
        """Network timeout raises RetryableTransportError."""
        envelope = _envelope()

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timeout")

        async with _client(timeout_handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(RetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_http_network_error_is_retryable(self) -> None:
        """Network error raises RetryableTransportError."""
        envelope = _envelope()

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.NetworkError("no network")

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(RetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_429_rate_limit_is_retryable(self) -> None:
        """HTTP 429 raises RetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(429)) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(RetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_401_auth_error_is_non_retryable(self) -> None:
        """HTTP 401 raises NonRetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(401)) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(NonRetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_403_forbidden_is_non_retryable(self) -> None:
        """HTTP 403 raises NonRetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(403)) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(NonRetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_4xx_client_error_is_non_retryable(self) -> None:
        """HTTP 400-range errors raise NonRetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(400)) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(NonRetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_5xx_server_error_is_retryable(self) -> None:
        """HTTP 5xx errors raise RetryableTransportError."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(500)) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                with pytest.raises(RetryableTransportError):
                    await send_welcome(envelope, _config(), http_client=client)

    async def test_success_returns_transport_result(self) -> None:
        """HTTP 200 returns a successful TransportResult."""
        envelope = _envelope()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "msg-1"})

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                result = await send_welcome(envelope, _config(), http_client=client)
                assert result.transport == "bundle"
                assert "chan-123" in result.detail
                assert result.http_status == 200

    async def test_sends_correct_headers(self) -> None:
        """Outbound request includes authorization and content-type headers."""
        envelope = _envelope()
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, json={"id": "msg-1"})

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret",
                return_value="bot-token-secret",
            ):
                await send_welcome(envelope, _config(), http_client=client)
                assert captured["auth"] == "Bot bot-token-secret"
                assert captured["content_type"] == "application/json"

    async def test_sends_correct_body(self) -> None:
        """Outbound request body includes the welcome message."""
        envelope = _envelope(payload={"text": "Welcome to the server!", "channel_id": "chan-1"})
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"id": "msg-1"})

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                await send_welcome(envelope, _config(), http_client=client)
                assert b"Welcome to the server!" in captured["body"]

    async def test_ssrf_guard_blocks_invalid_url(self) -> None:
        """SSRF-guarded request raises NonRetryableTransportError on blocked URL."""
        envelope = _envelope()
        async with _client(lambda r: httpx.Response(200)) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                from waddle_transports.url_guard import SSRFError

                with patch("builtin_handlers.social_welcome_action.guarded_request") as mock_guard:
                    mock_guard.side_effect = SSRFError("localhost blocked")
                    with pytest.raises(NonRetryableTransportError, match="SSRF"):
                        await send_welcome(envelope, _config(), http_client=client)

    async def test_non_string_text_is_non_retryable(self) -> None:
        """Non-string 'text' in payload raises NonRetryableTransportError."""
        envelope = _envelope(payload={"text": 123, "channel_id": "chan-1"})
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="text"):
                await send_welcome(envelope, _config(), http_client=client)

    async def test_empty_text_is_non_retryable(self) -> None:
        """Empty 'text' in payload raises NonRetryableTransportError."""
        envelope = _envelope(payload={"text": "", "channel_id": "chan-1"})
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="text"):
                await send_welcome(envelope, _config(), http_client=client)

    async def test_non_string_channel_id_ignored(self) -> None:
        """Non-string channel_id in event is treated as missing."""
        envelope = _envelope(payload={"text": "Welcome!", "channel_id": 123})
        config = _config(channel_id="config-chan")

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "msg-1"})

        async with _client(handler) as client:
            with patch(
                "builtin_handlers.social_welcome_action.resolve_secret", return_value="secret"
            ):
                await send_welcome(envelope, config, http_client=client)
                assert "config-chan" in captured.get("url", "")
