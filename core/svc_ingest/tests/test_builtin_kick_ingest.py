"""Tests for `builtin_handlers.kick_ingest` -- `normalize()` + Kick webhook verify/handler helpers.

`redis_client` (from `conftest.py`) is a real `fakeredis.FakeAsyncRedis` --
genuine LPUSH/RPOP round trip for the fan-out assertions, matching
`test_eventsub.py`/`test_fanout.py`'s own precedent for this container.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core import PlatformEvent
from flask_core.app_manifest import parse_manifest
from flask_core.app_registry import AppRegistry
from flask_core.stream_pipeline import bundle_stream_key

from builtin_handlers.kick_ingest import (
    CONSUMES_TAG,
    EVENTSUB_CONSUMES_TAG,
    KICK_WEBHOOK_EVENT_TYPE_MAP,
    STREAM_LIFECYCLE_EVENT_TYPES,
    handle_kick_webhook,
    normalize,
    verify_kick_webhook_signature,
)

WEBHOOK_SECRET = "test-kick-webhook-secret"  # noqa: S105 - test literal, not a secret
TENANT = "acme-corp"
EVENTSUB_APP_ID = "waddles.bot.kickevents.eventsub"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _eventsub_manifest(app_id: str = EVENTSUB_APP_ID) -> Any:
    """A throwaway `AppManifest` declaring an `ingest` stage consuming `EVENTSUB_CONSUMES_TAG`.

    Mirrors `test_fanout.py`'s own `_manifest` helper -- no real Kick
    EventSub ingest bundle exists yet (`builtin_handlers/kick_gateway_manifest.py`
    is out of this task's edit scope), so tests register one ad hoc to
    exercise the real `fan_out_event` LPUSH round trip.
    """
    return parse_manifest(
        {
            "app_id": app_id,
            "name": app_id,
            "version": "1.0.0",
            "feature": "waddles.bot.kickevents",
            "module": "bot",
            "provider": "builtin",
            "is_default": True,
            "stages": {
                "ingest": {
                    "entrypoint": "builtin_handlers.kick_ingest:normalize",
                    "consumes": [EVENTSUB_CONSUMES_TAG],
                }
            },
        }
    )


class TestConsumesTag:
    def test_re_exported_from_the_receiver(self) -> None:
        assert CONSUMES_TAG == "kick.message"


class TestNormalize:
    async def test_normalizes_a_real_chat_message(self) -> None:
        raw = {
            "platform": "kick",
            "text": "  hello chat  ",
            "chatroom_id": 12345,
            "channel_slug": "acme",
            "author_id": "999",
            "display_name": "PenguinFan",
            "badges": ["moderator", "subscriber"],
            "is_mod": True,
            "is_subscriber": True,
            "is_owner": False,
            "message_id": "msg-abc",
            "created_at": "2026-09-11T12:00:00.000000Z",
        }
        event = await normalize(raw)

        assert isinstance(event, PlatformEvent)
        assert event.platform == "kick"
        assert event.event_type == "message"
        assert event.actor == "999"
        assert event.payload["text"] == "hello chat"
        assert event.payload["chatroom_id"] == 12345
        assert event.payload["channel_slug"] == "acme"
        assert event.payload["author_id"] == "999"
        assert event.payload["display_name"] == "PenguinFan"
        assert event.payload["badges"] == ["moderator", "subscriber"]
        assert event.payload["is_mod"] is True
        assert event.payload["is_subscriber"] is True
        assert event.payload["is_owner"] is False
        assert event.payload["message_id"] == "msg-abc"
        assert event.occurred_at == "2026-09-11T12:00:00.000000Z"

    async def test_missing_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"chatroom_id": 1, "channel_slug": "acme"})

    async def test_missing_chatroom_id_raises(self) -> None:
        with pytest.raises(ValueError, match="chatroom_id"):
            await normalize({"text": "hi", "channel_slug": "acme"})

    async def test_missing_channel_slug_raises(self) -> None:
        with pytest.raises(ValueError, match="channel_slug"):
            await normalize({"text": "hi", "chatroom_id": 1})

    async def test_missing_optional_fields_default_absent_never_raise(self) -> None:
        raw = {"text": "hi", "chatroom_id": 1, "channel_slug": "acme"}
        event = await normalize(raw)

        assert event.actor is None
        assert event.payload["author_id"] is None
        assert event.payload["display_name"] is None
        assert event.payload["badges"] == []
        assert event.payload["is_mod"] is False
        assert event.payload["is_subscriber"] is False
        assert event.payload["is_owner"] is False
        assert event.payload["message_id"] is None
        assert event.occurred_at  # stamped with a UTC default

    async def test_non_list_badges_defaults_to_empty_list(self) -> None:
        raw = {"text": "hi", "chatroom_id": 1, "channel_slug": "acme", "badges": "not-a-list"}
        event = await normalize(raw)
        assert event.payload["badges"] == []

    async def test_falls_back_to_created_at_when_occurred_at_absent(self) -> None:
        raw = {
            "text": "hi",
            "chatroom_id": 1,
            "channel_slug": "acme",
            "created_at": "2026-01-01T00:00:00.000000Z",
        }
        event = await normalize(raw)
        assert event.occurred_at == "2026-01-01T00:00:00.000000Z"

    async def test_occurred_at_takes_precedence_over_created_at(self) -> None:
        raw = {
            "text": "hi",
            "chatroom_id": 1,
            "channel_slug": "acme",
            "created_at": "2026-01-01T00:00:00.000000Z",
            "occurred_at": "2026-02-02T00:00:00+00:00",
        }
        event = await normalize(raw)
        assert event.occurred_at == "2026-02-02T00:00:00+00:00"

    async def test_string_chatroom_id_is_accepted(self) -> None:
        raw = {"text": "hi", "chatroom_id": "12345", "channel_slug": "acme"}
        event = await normalize(raw)
        assert event.payload["chatroom_id"] == "12345"

    async def test_zero_chatroom_id_is_accepted_not_treated_as_missing(self) -> None:
        """`0` is a falsy int but a structurally valid chatroom id -- must not raise."""
        raw = {"text": "hi", "chatroom_id": 0, "channel_slug": "acme"}
        event = await normalize(raw)
        assert event.payload["chatroom_id"] == 0


class TestVerifyKickWebhookSignature:
    def test_valid_signature_verifies(self) -> None:
        body = b'{"type":"StreamStart"}'
        assert verify_kick_webhook_signature(body, _sign(body), WEBHOOK_SECRET) is True

    def test_invalid_signature_fails(self) -> None:
        body = b'{"type":"StreamStart"}'
        result = verify_kick_webhook_signature(body, "not-the-real-signature", WEBHOOK_SECRET)
        assert result is False

    def test_missing_signature_fails_closed(self) -> None:
        body = b'{"type":"StreamStart"}'
        assert verify_kick_webhook_signature(body, "", WEBHOOK_SECRET) is False

    def test_signature_for_different_body_fails(self) -> None:
        signature = _sign(b'{"type":"StreamStart"}')
        result = verify_kick_webhook_signature(b'{"type":"StreamEnd"}', signature, WEBHOOK_SECRET)
        assert result is False


class TestHandleKickWebhook:
    def _kwargs(self, redis_client: Any, *, registry: AppRegistry | None = None) -> dict[str, Any]:
        return {
            "redis_client": redis_client,
            "registry": registry if registry is not None else AppRegistry(),
            "tenant": TENANT,
        }

    async def test_unconfigured_secret_returns_503_without_verifying(
        self, redis_client: Any
    ) -> None:
        body = b'{"type":"StreamStart"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            {"type": "StreamStart"},
            secret="",
            **self._kwargs(redis_client),
        )
        assert status == 503
        assert "error" in response

    async def test_invalid_signature_returns_401(self, redis_client: Any) -> None:
        body = b'{"type":"StreamStart"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": "bad"},
            body,
            {"type": "StreamStart"},
            secret=WEBHOOK_SECRET,
            **self._kwargs(redis_client),
        )
        assert status == 401
        assert "error" in response

    async def test_missing_signature_header_returns_401(self, redis_client: Any) -> None:
        body = b'{"type":"StreamStart"}'
        response, status = await handle_kick_webhook(
            {},
            body,
            {"type": "StreamStart"},
            secret=WEBHOOK_SECRET,
            **self._kwargs(redis_client),
        )
        assert status == 401
        assert "error" in response

    @pytest.mark.parametrize(
        ("kick_type", "mapped_type"), sorted(KICK_WEBHOOK_EVENT_TYPE_MAP.items())
    )
    async def test_every_known_event_type_maps_correctly(
        self, kick_type: str, mapped_type: str, redis_client: Any
    ) -> None:
        body = f'{{"type":"{kick_type}"}}'.encode()
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            {"type": kick_type},
            secret=WEBHOOK_SECRET,
            **self._kwargs(redis_client),
        )
        assert status == 200
        assert response == {"received": True, "event_type": mapped_type}

    async def test_unknown_event_type_maps_to_unknown_not_rejected(self, redis_client: Any) -> None:
        body = b'{"type":"SomeFutureEventType"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            {"type": "SomeFutureEventType"},
            secret=WEBHOOK_SECRET,
            **self._kwargs(redis_client),
        )
        assert status == 200
        assert response == {"received": True, "event_type": "unknown"}

    async def test_missing_type_field_maps_to_unknown(self, redis_client: Any) -> None:
        body = b"{}"
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            {},
            secret=WEBHOOK_SECRET,
            **self._kwargs(redis_client),
        )
        assert status == 200
        assert response == {"received": True, "event_type": "unknown"}


class TestHandleKickWebhookStreamLifecycleFanOut:
    """Gh #287 S10 -- `StreamStart`/`StreamEnd` fan a raw live ON/OFF event out."""

    def test_stream_lifecycle_event_types_map_to_generic_platform_event_types(self) -> None:
        assert STREAM_LIFECYCLE_EVENT_TYPES == {"StreamStart", "StreamEnd"}

    async def test_stream_start_fans_out_when_a_consumer_is_registered(
        self, redis_client: Any
    ) -> None:
        registry = AppRegistry()
        registry.register(_eventsub_manifest())
        body_json = {
            "type": "StreamStart",
            "channel_slug": "acme",
            "channel_id": "555",
            "started_at": "2026-09-11T12:00:00Z",
            "viewer_count": 42,
        }
        body = b'{"type":"StreamStart"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            body_json,
            secret=WEBHOOK_SECRET,
            redis_client=redis_client,
            registry=registry,
            tenant=TENANT,
        )
        assert status == 200
        assert response == {"received": True, "event_type": "stream_start"}

        ingest_key = bundle_stream_key(TENANT, None, EVENTSUB_APP_ID, "ingest")
        raw = await redis_client.rpop(ingest_key)
        assert raw is not None
        event = json.loads(raw)
        assert event["platform"] == "kick"
        assert event["event_type"] == "stream.online"
        assert event["payload"] == {
            "channel_slug": "acme",
            "channel_id": "555",
            "started_at": "2026-09-11T12:00:00Z",
            "viewer_count": 42,
        }

    async def test_stream_end_fans_out_when_a_consumer_is_registered(
        self, redis_client: Any
    ) -> None:
        registry = AppRegistry()
        registry.register(_eventsub_manifest())
        body_json = {"type": "StreamEnd", "channel_slug": "acme", "channel_id": "555"}
        body = b'{"type":"StreamEnd"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            body_json,
            secret=WEBHOOK_SECRET,
            redis_client=redis_client,
            registry=registry,
            tenant=TENANT,
        )
        assert status == 200
        assert response == {"received": True, "event_type": "stream_end"}

        ingest_key = bundle_stream_key(TENANT, None, EVENTSUB_APP_ID, "ingest")
        raw = await redis_client.rpop(ingest_key)
        assert raw is not None
        event = json.loads(raw)
        assert event["event_type"] == "stream.offline"
        assert event["payload"]["channel_slug"] == "acme"
        assert event["payload"]["channel_id"] == "555"
        assert event["payload"]["started_at"] is None
        assert event["payload"]["viewer_count"] is None

    async def test_stream_lifecycle_event_with_no_consumers_still_acks(
        self, redis_client: Any
    ) -> None:
        """No manifest declares `EVENTSUB_CONSUMES_TAG` yet -- 0 consumers, never fatal."""
        body_json = {"type": "StreamStart", "channel_slug": "acme", "channel_id": "555"}
        body = b'{"type":"StreamStart"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            body_json,
            secret=WEBHOOK_SECRET,
            redis_client=redis_client,
            registry=AppRegistry(),
            tenant=TENANT,
        )
        assert status == 200
        assert response == {"received": True, "event_type": "stream_start"}

    async def test_a_non_lifecycle_event_never_calls_fan_out(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fan_out_mock = AsyncMock(return_value=0)
        monkeypatch.setattr("builtin_handlers.kick_ingest.fan_out_event", fan_out_mock)

        body = b'{"type":"Subscription"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            {"type": "Subscription"},
            secret=WEBHOOK_SECRET,
            redis_client=redis_client,
            registry=AppRegistry(),
            tenant=TENANT,
        )
        assert status == 200
        assert response == {"received": True, "event_type": "subscription"}
        fan_out_mock.assert_not_awaited()

    async def test_fanout_failure_is_caught_and_still_acks(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One bad/unresolvable fan-out must never fail the webhook ack."""
        monkeypatch.setattr(
            "builtin_handlers.kick_ingest.fan_out_event",
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        body = b'{"type":"StreamStart"}'
        response, status = await handle_kick_webhook(
            {"X-Kick-Signature": _sign(body)},
            body,
            {"type": "StreamStart", "channel_slug": "acme", "channel_id": "555"},
            secret=WEBHOOK_SECRET,
            redis_client=redis_client,
            registry=AppRegistry(),
            tenant=TENANT,
        )
        assert status == 200
        assert response == {"received": True, "event_type": "stream_start"}
