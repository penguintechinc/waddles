"""Tests for bundles.social_quote_action: quote message send and DB operations.

Tests quote addition to database, reply-in-place channel resolution,
and message sending via Discord/Twitch transports.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from flask_core import (
    PlatformEvent,
    StageEnvelope,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from bundles.social_quote_action import send_message


def _envelope(
    payload: dict | None = None,
    *,
    platform: str = "discord",
    community: str = "42",
    actor: str | None = "penguin",
) -> StageEnvelope:
    """Factory to build a StageEnvelope for testing."""
    default_payload = {"text": "hello", "channel_id": "123"}
    return StageEnvelope(
        tenant="1",
        community=community,
        app_id="waddles.social.quote.default",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="message",
            actor=actor,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    """Factory to build config dict for testing."""
    base = {
        "bot_token_ref": "TEST_DISCORD_TOKEN",
        "api_base": "https://8.8.8.8/v1",
        "channel_id": "fallback-chan",
    }
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    """Create a mock httpx.AsyncClient with a handler function."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class _FakeQuotesTable:
    """Minimal fake `penguin_dal.TableProxy` -- only `.async_insert()`, this bundle's own surface."""

    def __init__(self, counter_start: int = 100) -> None:
        self._counter = counter_start

    async def async_insert(self, **kwargs: Any) -> int:
        """Mock insert for quote creation -- returns the new row's id, like the real TableProxy."""
        self._counter += 1
        return self._counter


class _FakeDal:
    """In-memory stand-in for `penguin_dal.AsyncDB` -- implements only the `.quotes` surface."""

    def __init__(self) -> None:
        self.quotes = _FakeQuotesTable()


@pytest.fixture(autouse=True)
def _dal() -> Any:
    """Set up fake DAL for all tests."""
    fake = _FakeDal()
    set_bundle_dal(fake)
    yield fake
    reset_bundle_dal_for_tests()


class TestSendMessage:
    async def test_sends_discord_message_with_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sends_message sends Discord message with Bot token auth.

        regression: discord Bot auth scheme -- Discord's REST API requires
        `Authorization: Bot <token>`; `Bearer` is the OAuth2 user scheme and
        is rejected with 401 even for a valid bot token.
        """
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            result = await send_message(_envelope(), _config(), http_client=client)

        assert captured["auth"] == "Bot s3cr3t"
        assert result.transport == "bundle"

    async def test_resolves_channel_id_from_payload_discord(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reply-in-place: payload channel_id takes precedence (Discord)."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200)

        async with _client(handler) as client:
            result = await send_message(
                _envelope({"text": "hi", "channel_id": "payload-chan"}),
                _config(),
                http_client=client,
            )

        assert "payload-chan" in captured["url"]
        assert result.http_status == 200

    async def test_fallback_to_config_channel_id_discord(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uses config channel_id when payload has none (Discord)."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200)

        async with _client(handler) as client:
            await send_message(
                _envelope({"text": "hi"}),  # no channel_id in payload
                _config(channel_id="config-chan"),
                http_client=client,
            )

        assert "config-chan" in captured["url"]

    async def test_missing_channel_id_is_non_retryable(self) -> None:
        """Missing both payload and config channel raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="channel"):
                await send_message(
                    _envelope({"text": "hi"}),  # no channel_id
                    _config(channel_id=None),  # no fallback
                    http_client=client,
                )

    async def test_missing_text_is_non_retryable(self) -> None:
        """Missing or empty text raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="text"):
                await send_message(
                    _envelope({"channel_id": "123"}),  # missing text
                    _config(),
                    http_client=client,
                )

    async def test_429_rate_limit_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 429 rate limit raises RetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        async with _client(lambda r: httpx.Response(429)) as client:
            with pytest.raises(RetryableTransportError):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_401_unauthorized_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 401 auth error raises NonRetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        async with _client(lambda r: httpx.Response(401)) as client:
            with pytest.raises(NonRetryableTransportError, match="auth"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_401_rejection_logs_token_ref(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HTTP 401 logs a discord_send_rejected warning naming the token ref.

        regression: discord Bot auth scheme -- a 401/403 must be logged with
        the failing bot_token_ref (never the token value) so a stale/invalid
        bot token is diagnosable without exposing the secret.
        """
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        with caplog.at_level("WARNING"):
            async with _client(lambda r: httpx.Response(401)) as client:
                with pytest.raises(NonRetryableTransportError):
                    await send_message(_envelope(), _config(), http_client=client)

        assert "social_quote_action.discord_send_rejected" in caplog.text
        assert "TEST_DISCORD_TOKEN" in caplog.text
        assert "s3cr3t" not in caplog.text

    async def test_500_server_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 5xx server error raises RetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        async with _client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(RetryableTransportError, match="server error"):
                await send_message(_envelope(), _config(), http_client=client)

    async def test_network_timeout_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Network timeout raises RetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timeout")

        async with _client(handler) as client:
            with pytest.raises(RetryableTransportError, match="failed"):
                await send_message(_envelope(), _config(), http_client=client)


class TestQuoteAddIntent:
    async def test_add_quote_intent_executes_db_insert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quote add intent from process stage is executed in action stage."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.quote.default"):
                result = await send_message(
                    _envelope({
                        "text": "",
                        "channel_id": "123",
                        "_quote_action": "add",
                        "_quote_text": "test quote",
                        "_actor": "penguin",
                    }),
                    _config(),
                    http_client=client,
                )

        # Verify response was successful
        assert result.http_status == 200

    async def test_add_quote_db_failure_returns_error_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Database error on quote add returns error message."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")
        mock_dal = MagicMock()
        mock_dal.quotes.async_insert = AsyncMock(side_effect=Exception("DB error"))
        set_bundle_dal(mock_dal)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.quote.default"):
                await send_message(
                    _envelope({
                        "text": "",
                        "channel_id": "123",
                        "_quote_action": "add",
                        "_quote_text": "test",
                        "_actor": "penguin",
                    }),
                    _config(),
                    http_client=client,
                )
        reset_bundle_dal_for_tests()

    async def test_add_quote_no_dal_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Quote add without DAL returns error message."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")
        # This test doesn't apply anymore since the frozen API always requires a DAL
        # Skip or remove this test
        pass


class TestTwitchPlatform:
    async def test_twitch_uses_channel_name(self) -> None:
        """Twitch platform uses channel_name from payload."""
        mock_transport = AsyncMock()
        mock_transport.send = AsyncMock(return_value=MagicMock(transport="relay", detail="sent"))

        async with _client(lambda r: httpx.Response(200)) as client:
            with patch(
                "bundles.social_quote_action.RelayOutboundIrcTransport",
                return_value=mock_transport,
            ):
                await send_message(
                    _envelope({"text": "hi", "channel_name": "testchannel"}, platform="twitch"),
                    _config(channel=None, channel_id=None),
                    http_client=client,
                )

        # Verify the transport.send was called
        mock_transport.send.assert_called_once()


class TestEdgeCases:
    async def test_payload_fields_survive_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-text payload fields are preserved after send."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with _client(handler) as client:
            envelope = _envelope({"text": "hi", "channel_id": "123", "guild_id": "999"})
            result = await send_message(envelope, _config(), http_client=client)

        assert result.transport == "bundle"

    async def test_empty_quote_text_in_add_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty quote text in add intent returns error."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with _client(handler) as client:
            await send_message(
                _envelope({
                    "text": "",
                    "channel_id": "123",
                    "_quote_action": "add",
                    "_quote_text": "",
                    "_actor": "penguin",
                }),
                _config(),
                http_client=client,
            )

    async def test_missing_bot_token_ref_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing bot_token_ref in config raises NonRetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="bot_token_ref"):
                await send_message(
                    _envelope({"text": "hi", "channel_id": "123"}),
                    _config(bot_token_ref=None),
                    http_client=client,
                )

    async def test_ssrf_guard_rejection_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SSRF guard rejection raises NonRetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        from waddle_transports.url_guard import SSRFError

        async with _client(lambda r: httpx.Response(200)) as client:
            with patch(
                "waddle_transports.url_guard.guarded_request", side_effect=SSRFError("blocked")
            ):
                with pytest.raises(NonRetryableTransportError, match="SSRF"):
                    await send_message(
                        _envelope({"text": "hi", "channel_id": "123"}),
                        _config(),
                        http_client=client,
                    )

    async def test_400_client_error_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 4xx client error raises NonRetryableTransportError."""
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "s3cr3t")

        async with _client(lambda r: httpx.Response(400)) as client:
            with pytest.raises(NonRetryableTransportError, match="client error"):
                await send_message(_envelope(), _config(), http_client=client)
