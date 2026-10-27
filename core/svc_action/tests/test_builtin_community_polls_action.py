"""Tests for `builtin_handlers.community_polls_action.send_poll_reply`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from flask_core import (
    PlatformEvent,
    StageEnvelope,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_transports import NonRetryableTransportError

from builtin_handlers.community_polls_action import send_poll_reply


def _envelope(payload: dict | None = None, channel_id: str | None = None) -> StageEnvelope:
    """Create a test StageEnvelope with poll reply."""
    default_payload = {
        "text": "Poll created: ID 1",
        "channel_id": channel_id or "123",
    }
    if payload is not None:
        default_payload.update(payload)

    return StageEnvelope(
        tenant="t1",
        community="c1",
        app_id="waddles.community.polls.default",
        stage="action",
        event=PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload=default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    """Create a test config."""
    base: dict = {}
    base.update(overrides)
    return base


class _FakeDal:
    """Minimal fake DAL for action bundle tests."""

    async def execute(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """No-op execute for action tests."""
        return []


@pytest.fixture(autouse=True)
def _dal() -> Any:
    """Set up and tear down fake DAL for each test."""
    fake = _FakeDal()
    set_bundle_dal(fake)
    yield fake
    reset_bundle_dal_for_tests()


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    """Create a test HTTP client with a mock transport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class TestSendPollReply:
    """Tests for send_poll_reply entrypoint."""

    async def test_sends_with_event_channel_id(self, _dal: _FakeDal) -> None:
        """Should send when channel_id is in event payload."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(_envelope(), _config(), http_client=client)
        assert result.transport == "bundle"
        assert result.http_status == 200
        assert "channel=123" in result.detail

    async def test_sends_with_config_fallback_channel_id(self, _dal: _FakeDal) -> None:
        """Should send with config channel_id when event has none."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(
                    _envelope(payload={"text": "hello", "channel_id": None}),
                    _config(channel_id="456"),
                    http_client=client,
                )
        assert result.transport == "bundle"
        assert "channel=456" in result.detail

    async def test_sends_with_event_channel_name_twitch(self, _dal: _FakeDal) -> None:
        """Should send when channel_name is in event payload (Twitch)."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(
                    _envelope(
                        payload={
                            "text": "hello",
                            "channel_id": None,
                            "channel_name": "waddles_tv",
                        }
                    ),
                    _config(),
                    http_client=client,
                )
        assert result.transport == "bundle"
        assert "channel=waddles_tv" in result.detail

    async def test_missing_text_raises_non_retryable(self, _dal: _FakeDal) -> None:
        """Missing text field should raise NonRetryableTransportError."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                # Create envelope without text by replacing it with None
                env = _envelope()
                env = StageEnvelope(
                    tenant=env.tenant,
                    community=env.community,
                    app_id=env.app_id,
                    stage=env.stage,
                    event=PlatformEvent(
                        platform=env.event.platform,
                        event_type=env.event.event_type,
                        actor=env.event.actor,
                        payload={"channel_id": "123"},  # No text
                        occurred_at=env.event.occurred_at,
                    ),
                    ts=env.ts,
                )
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_poll_reply(env, _config(), http_client=client)

    async def test_empty_text_raises_non_retryable(self, _dal: _FakeDal) -> None:
        """Empty text field should raise NonRetryableTransportError."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_poll_reply(
                        _envelope(payload={"text": "", "channel_id": "123"}),
                        _config(),
                        http_client=client,
                    )

    async def test_missing_channel_raises_non_retryable(self, _dal: _FakeDal) -> None:
        """Missing channel_id and channel_name should raise NonRetryableTransportError."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                with pytest.raises(NonRetryableTransportError, match="channel"):
                    await send_poll_reply(
                        _envelope(payload={"text": "hello", "channel_id": None}),
                        _config(),
                        http_client=client,
                    )

    async def test_preserves_channel_id_from_event_over_config(self, _dal: _FakeDal) -> None:
        """Event channel_id should take precedence over config."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(
                    _envelope(channel_id="event-chan"),
                    _config(channel_id="config-chan"),
                    http_client=client,
                )
        assert "channel=event-chan" in result.detail

    async def test_includes_text_length_in_detail(self, _dal: _FakeDal) -> None:
        """Result detail should include text length."""
        long_text = "x" * 100
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(
                    _envelope(payload={"text": long_text, "channel_id": "123"}),
                    _config(),
                    http_client=client,
                )
        assert "100 chars" in result.detail

    async def test_non_string_text_raises_non_retryable(self, _dal: _FakeDal) -> None:
        """Non-string text should raise NonRetryableTransportError."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_poll_reply(
                        _envelope(payload={"text": 123, "channel_id": "123"}),
                        _config(),
                        http_client=client,
                    )

    async def test_non_string_channel_id_ignored_for_config(self, _dal: _FakeDal) -> None:
        """Non-string channel_id in config should be ignored."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            with pytest.raises(NonRetryableTransportError):
                async with _client(lambda r: httpx.Response(200)) as client:
                    await send_poll_reply(
                        _envelope(payload={"text": "hello", "channel_id": None}),
                        _config(channel_id=123),
                        http_client=client,
                    )

    async def test_result_transport_is_bundle(self, _dal: _FakeDal) -> None:
        """Result should have transport='bundle'."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(_envelope(), _config(), http_client=client)
        assert result.transport == "bundle"

    async def test_result_http_status_is_200(self, _dal: _FakeDal) -> None:
        """Result should have http_status=200."""
        with bundle_context(tenant="t1", community="c1", app_id="waddles.community.polls.default"):
            async with _client(lambda r: httpx.Response(200)) as client:
                result = await send_poll_reply(_envelope(), _config(), http_client=client)
        assert result.http_status == 200
