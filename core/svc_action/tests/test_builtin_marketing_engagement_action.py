"""Tests for `builtin_handlers.marketing_engagement_action.send_engagement_notification`."""

from __future__ import annotations

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.marketing_engagement_action import send_engagement_notification


def _envelope(payload: dict[str, object] | None = None) -> StageEnvelope:
    """Create a test StageEnvelope for engagement."""
    default_payload = {"text": "poll created", "channel_id": "123456789"}
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.marketing.engagement.default",
        stage="action",
        event=PlatformEvent(
            platform="discord",
            event_type="poll_create",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-09-04T12:00:00Z",
        ),
        ts="2026-09-04T12:00:00Z",
    )


def _config(**overrides: object) -> dict[str, object]:
    """Create a test config dict."""
    base: dict[str, object] = {
        "notification_token_ref": "TEST_ENGAGEMENT_TOKEN",
        "api_base": "https://8.8.8.8/v1",  # literal IP -- no real DNS in unit tests
    }
    base.update(overrides)
    return base


def _client(
    handler: object,
) -> httpx.AsyncClient:
    """Create a test HTTP client with mock transport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class TestChannelIdResolution:
    """Reply-in-place: payload.channel_id is primary, config.channel_id is fallback."""

    async def test_no_channel_id_from_either_source_is_non_retryable(self) -> None:
        """Missing channel_id from both payload and config raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="channel_id"):
                await send_engagement_notification(
                    _envelope(payload={"text": "poll"}), _config(), http_client=client
                )

    async def test_payload_channel_id_takes_precedence_over_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Payload channel_id takes precedence over config fallback."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t-token")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        payload = {"text": "poll", "channel_id": "from-payload"}
        async with _client(handler) as client:
            await send_engagement_notification(
                _envelope(payload=payload),
                _config(channel_id="from-config"),
                http_client=client,
            )
        assert captured["url"] == "https://8.8.8.8/v1/channels/from-payload/notifications"

    async def test_config_channel_id_used_as_fallback_when_payload_lacks_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config channel_id is used when payload lacks one."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t-token")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            await send_engagement_notification(
                _envelope(payload={"text": "poll"}),
                _config(channel_id="from-config"),
                http_client=client,
            )
        assert captured["url"] == "https://8.8.8.8/v1/channels/from-config/notifications"


class TestTokenResolution:
    """Token ref resolution and missing token handling."""

    async def test_missing_token_ref_is_non_retryable(self) -> None:
        """Missing notification_token_ref raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="notification_token_ref"):
                await send_engagement_notification(
                    _envelope(), _config(notification_token_ref=""), http_client=client
                )

    async def test_unresolvable_token_raises_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unresolvable token ref raises NonRetryableTransportError."""
        # Leave env var unset so resolution fails
        monkeypatch.delenv("TEST_ENGAGEMENT_TOKEN", raising=False)
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="token resolution"):
                await send_engagement_notification(_envelope(), _config(), http_client=client)


class TestEngagementPayload:
    """Engagement payload and notification body handling."""

    async def test_sends_engagement_notification_with_basic_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Basic engagement notification is sent with text and channel_id."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            result = await send_engagement_notification(_envelope(), _config(), http_client=client)
        assert result.transport == "bundle"
        assert "123456789" in result.detail

    async def test_poll_id_included_in_notification_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """poll_id from payload is included in notification body."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "1"})

        payload = {"text": "poll created", "channel_id": "123", "poll_id": "poll-456"}
        async with _client(handler) as client:
            await send_engagement_notification(
                _envelope(payload=payload), _config(), http_client=client
            )
        assert captured["body"]["poll_id"] == "poll-456"

    async def test_form_id_included_in_notification_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """form_id from payload is included in notification body."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "1"})

        payload = {"text": "form submitted", "channel_id": "123", "form_id": "form-789"}
        async with _client(handler) as client:
            await send_engagement_notification(
                _envelope(payload=payload), _config(), http_client=client
            )
        assert captured["body"]["form_id"] == "form-789"

    async def test_options_included_in_notification_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Poll options from payload are included in notification body."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "1"})

        payload = {
            "text": "poll options",
            "channel_id": "123",
            "options": ["yes", "no", "maybe"],
        }
        async with _client(handler) as client:
            await send_engagement_notification(
                _envelope(payload=payload), _config(), http_client=client
            )
        assert captured["body"]["options"] == ["yes", "no", "maybe"]


class TestErrorHandling:
    """HTTP error handling and retry classification."""

    async def test_missing_text_is_non_retryable(self) -> None:
        """Missing text field raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="text"):
                await send_engagement_notification(
                    _envelope(payload={"channel_id": "123"}), _config(), http_client=client
                )

    async def test_429_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 429 (rate limit) is classified as retryable."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        async with _client(lambda r: httpx.Response(429)) as client:
            with pytest.raises(RetryableTransportError):
                await send_engagement_notification(_envelope(), _config(), http_client=client)

    async def test_401_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 401 (auth failure) is classified as non-retryable."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        async with _client(lambda r: httpx.Response(401)) as client:
            with pytest.raises(NonRetryableTransportError):
                await send_engagement_notification(_envelope(), _config(), http_client=client)

    async def test_403_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 403 (forbidden) is classified as non-retryable."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        async with _client(lambda r: httpx.Response(403)) as client:
            with pytest.raises(NonRetryableTransportError):
                await send_engagement_notification(_envelope(), _config(), http_client=client)

    async def test_500_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 500 (server error) is classified as retryable."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")
        async with _client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(RetryableTransportError):
                await send_engagement_notification(_envelope(), _config(), http_client=client)

    async def test_network_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Network errors are classified as retryable."""
        monkeypatch.setenv("TEST_ENGAGEMENT_TOKEN", "s3cr3t")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.NetworkError("connection refused")

        async with _client(handler) as client:
            with pytest.raises(RetryableTransportError):
                await send_engagement_notification(_envelope(), _config(), http_client=client)
