"""builtin_handlers/twitch_send_action.py -- OUTBOUND `irc` transport relay, Valkey-backed.

`_get_redis_client` is monkeypatched to a real `fakeredis.FakeAsyncRedis`
-- genuine LPUSH semantics exercised end to end (this handler -> the
shared `waddle_transports.transports.irc_relay.RelayOutboundIrcTransport`
-> the real Valkey key `outbound_drain.py`'s drain loop reads), not a
mocked call.
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis
import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError
from waddle_transports.transports.irc_relay import outbound_queue_key

import builtin_handlers.twitch_send_action as twitch_bundle
from builtin_handlers.twitch_send_action import _get_redis_client, send_message


def _envelope(payload: dict | None = None) -> StageEnvelope:
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.twitch.default",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat_message",
            actor=None,
            payload=payload if payload is not None else {"text": "hello from waddlebot"},
            occurred_at="2026-09-02T12:00:00Z",
        ),
        ts="2026-09-02T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base = {"channel": "fallback-channel"}
    base.update(overrides)
    return base


@pytest.fixture
async def fake_redis() -> Any:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _patch_redis_client(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setattr(twitch_bundle, "_get_redis_client", lambda config: fake_redis)


def _noop_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


class TestChannelResolution:
    """Reply-in-place: payload.channel_name is primary, config.channel is a fallback only."""

    async def test_no_channel_from_either_source_is_non_retryable(self) -> None:
        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="channel"):
                await send_message(
                    _envelope(payload={"text": "hi"}), _config(channel=None), http_client=client
                )

    async def test_payload_channel_name_takes_precedence_over_config(self, fake_redis: Any) -> None:
        payload = {"text": "reply!", "channel_name": "origin-channel"}
        async with _noop_http_client() as client:
            result = await send_message(
                _envelope(payload=payload), _config(channel="from-config"), http_client=client
            )

        assert result.transport == "irc_relay"
        assert "origin-channel" in result.detail
        raw = await fake_redis.rpop(outbound_queue_key("twitch"))
        assert json.loads(raw) == {"channel": "origin-channel", "text": "reply!"}

    async def test_config_channel_used_as_fallback_when_payload_lacks_one(
        self, fake_redis: Any
    ) -> None:
        async with _noop_http_client() as client:
            result = await send_message(
                _envelope(payload={"text": "scheduled announcement"}),
                _config(channel="announcements"),
                http_client=client,
            )

        assert "announcements" in result.detail
        raw = await fake_redis.rpop(outbound_queue_key("twitch"))
        assert json.loads(raw) == {"channel": "announcements", "text": "scheduled announcement"}


async def test_missing_payload_text_is_non_retryable() -> None:
    async with _noop_http_client() as client:
        with pytest.raises(NonRetryableTransportError, match="'text'"):
            await send_message(
                _envelope(payload={"channel_name": "waddlebot"}), _config(), http_client=client
            )


def test_get_redis_client_builds_and_caches_a_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real (unpatched) `_get_redis_client`.

    Production path, not exercised by any monkeypatched test above.
    """
    monkeypatch.setattr(twitch_bundle, "_redis_client", None)
    monkeypatch.setenv("VALKEY_URL", "redis://example-valkey:6379/0")

    client = _get_redis_client({})

    assert client is not None
    # Cached -- a second call returns the SAME instance, not a fresh connection.
    assert _get_redis_client({}) is client


async def test_transport_non_retryable_error_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `NonRetryableTransportError` the shared transport raises propagates directly.

    `runner.py::_handle_envelope` only ever catches `waddle_transports`'
    own error types -- there is no local translation layer to go through.
    """

    async def _raise(self, config, payload):  # noqa: ANN001, ANN202, ARG001
        raise NonRetryableTransportError("bad channel", http_status=400)

    monkeypatch.setattr(twitch_bundle.RelayOutboundIrcTransport, "send", _raise)

    async with _noop_http_client() as client:
        with pytest.raises(NonRetryableTransportError, match="bad channel"):
            await send_message(
                _envelope(payload={"text": "hi", "channel_name": "waddlebot"}),
                _config(),
                http_client=client,
            )


async def test_transport_retryable_error_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(self, config, payload):  # noqa: ANN001, ANN202, ARG001
        raise RetryableTransportError("valkey unavailable", http_status=503)

    monkeypatch.setattr(twitch_bundle.RelayOutboundIrcTransport, "send", _raise)

    async with _noop_http_client() as client:
        with pytest.raises(RetryableTransportError, match="valkey unavailable"):
            await send_message(
                _envelope(payload={"text": "hi", "channel_name": "waddlebot"}),
                _config(),
                http_client=client,
            )
