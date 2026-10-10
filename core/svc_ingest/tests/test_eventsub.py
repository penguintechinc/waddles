"""Tests for `eventsub.TwitchEventSubHandler`/`verify_signature`/`build_raw_event`.

`redis_client` is a real `fakeredis.FakeAsyncRedis` -- genuine LPUSH/RPOP
round trip for the fan-out assertions, matching this container's other
receiver tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask_core.app_registry import AppRegistry
from flask_core.stream_pipeline import bundle_stream_key

from builtin_handlers.twitch_gateway_manifest import register_default_bundles
from eventsub import (
    DEFAULT_SUBSCRIPTION_TYPES,
    EVENTSUB_MESSAGE_ID,
    EVENTSUB_MESSAGE_TYPE,
    EVENTSUB_SIGNATURE,
    EVENTSUB_TIMESTAMP,
    TwitchEventSubHandler,
    build_raw_event,
    verify_signature,
)

TENANT = "acme-corp"
SECRET = "s3cr3t-eventsub-secret"  # noqa: S105 - test literal, not a real secret
APP_ID = "waddles.bot.twitchevents.eventsub"


def _signed_headers(*, message_id: str, timestamp: str, body: bytes) -> dict[str, str]:
    message = message_id.encode() + timestamp.encode() + body
    signature = "sha256=" + hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    return {
        EVENTSUB_MESSAGE_ID: message_id,
        EVENTSUB_TIMESTAMP: timestamp,
        EVENTSUB_SIGNATURE: signature,
    }


class TestVerifySignature:
    def test_valid_signature_passes(self) -> None:
        body = b'{"a": 1}'
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        assert verify_signature(secret=SECRET, headers=headers, body=body) is True

    def test_wrong_secret_fails(self) -> None:
        body = b'{"a": 1}'
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        assert verify_signature(secret="wrong-secret", headers=headers, body=body) is False

    def test_tampered_body_fails(self) -> None:
        headers = _signed_headers(message_id="m1", timestamp="t1", body=b'{"a": 1}')
        assert verify_signature(secret=SECRET, headers=headers, body=b'{"a": 2}') is False

    def test_missing_headers_fail_closed(self) -> None:
        assert verify_signature(secret=SECRET, headers={}, body=b"{}") is False


class TestDefaultSubscriptionTypes:
    def test_includes_stream_online_and_offline(self) -> None:
        """Gh #287 S10 -- the webhook-side filter must not drop a real Twitch delivery."""
        assert "stream.online" in DEFAULT_SUBSCRIPTION_TYPES
        assert "stream.offline" in DEFAULT_SUBSCRIPTION_TYPES


class TestBuildRawEvent:
    def test_follow_event_shape(self) -> None:
        raw = build_raw_event(
            "channel.follow",
            {"broadcaster_user_id": "999", "user_id": "555", "user_login": "alice"},
            {},
        )
        assert raw["event_type"] == "channel.follow"
        assert raw["broadcaster_id"] == "999"
        assert raw["user_login"] == "alice"

    def test_raid_event_metadata(self) -> None:
        raw = build_raw_event(
            "channel.raid",
            {"from_broadcaster_user_id": "111", "viewers": 42},
            {},
        )
        assert raw["metadata"] == {"viewers": 42}
        assert raw["user_id"] == "111"

    def test_stream_online_event_metadata(self) -> None:
        """Gh #287 S10 -- `started_at`/`type` ride in `metadata`, no `user_*` identity."""
        raw = build_raw_event(
            "stream.online",
            {
                "broadcaster_user_id": "999",
                "broadcaster_user_login": "waddlebot",
                "type": "live",
                "started_at": "2026-09-11T12:00:00Z",
            },
            {},
        )
        assert raw["event_type"] == "stream.online"
        assert raw["broadcaster_id"] == "999"
        assert raw["broadcaster_login"] == "waddlebot"
        assert raw["user_id"] is None
        assert raw["metadata"] == {"type": "live", "started_at": "2026-09-11T12:00:00Z"}

    def test_stream_online_event_defaults_type_when_absent(self) -> None:
        raw = build_raw_event("stream.online", {"broadcaster_user_id": "999"}, {})
        assert raw["metadata"] == {"type": "live", "started_at": ""}

    def test_stream_offline_event_metadata_is_empty(self) -> None:
        raw = build_raw_event(
            "stream.offline",
            {"broadcaster_user_id": "999", "broadcaster_user_login": "waddlebot"},
            {},
        )
        assert raw["event_type"] == "stream.offline"
        assert raw["broadcaster_id"] == "999"
        assert raw["metadata"] == {}


class TestTwitchEventSubHandler:
    def _handler(
        self, redis_client: Any, registry: AppRegistry | None = None
    ) -> TwitchEventSubHandler:
        reg = registry if registry is not None else AppRegistry()
        if not reg.all_apps():
            register_default_bundles(reg)
        return TwitchEventSubHandler(
            secret=SECRET, redis_client=redis_client, registry=reg, tenant_slug=TENANT
        )

    async def test_invalid_signature_returns_403(self, redis_client: Any) -> None:
        handler = self._handler(redis_client)
        body_json = {"challenge": "abc"}
        response, status = await handler.handle_webhook(headers={}, body=b"{}", body_json=body_json)
        assert status == 403
        assert "error" in response

    async def test_verification_challenge_is_echoed_back(self, redis_client: Any) -> None:
        handler = self._handler(redis_client)
        body = json.dumps({"challenge": "verify-me-123"}).encode()
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        headers[EVENTSUB_MESSAGE_TYPE] = "webhook_callback_verification"

        response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json={"challenge": "verify-me-123"}
        )
        assert status == 200
        assert response == {"challenge": "verify-me-123"}

    async def test_notification_fans_out_a_known_event_type(self, redis_client: Any) -> None:
        handler = self._handler(redis_client)
        body_json = {
            "subscription": {"type": "channel.follow", "condition": {}},
            "event": {"broadcaster_user_id": "999", "user_id": "555", "user_login": "alice"},
        }
        body = json.dumps(body_json).encode()
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        headers[EVENTSUB_MESSAGE_TYPE] = "notification"

        response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json=body_json
        )
        assert status == 200
        assert response == {"status": "ok"}

        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        raw = await redis_client.rpop(ingest_key)
        assert raw is not None
        event = json.loads(raw)
        assert event["event_type"] == "channel.follow"

    async def test_notification_fans_out_a_stream_online_event(self, redis_client: Any) -> None:
        """Gh #287 S10 -- a signed `stream.online` webhook reaches the ingest queue."""
        handler = self._handler(redis_client)
        body_json = {
            "subscription": {
                "type": "stream.online",
                "condition": {"broadcaster_user_id": "999"},
            },
            "event": {
                "broadcaster_user_id": "999",
                "broadcaster_user_login": "waddlebot",
                "type": "live",
                "started_at": "2026-09-11T12:00:00Z",
            },
        }
        body = json.dumps(body_json).encode()
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        headers[EVENTSUB_MESSAGE_TYPE] = "notification"

        response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json=body_json
        )
        assert status == 200
        assert response == {"status": "ok"}

        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        raw = await redis_client.rpop(ingest_key)
        assert raw is not None
        event = json.loads(raw)
        assert event["event_type"] == "stream.online"
        assert event["broadcaster_id"] == "999"
        assert event["metadata"] == {"type": "live", "started_at": "2026-09-11T12:00:00Z"}

    async def test_notification_fans_out_a_stream_offline_event(self, redis_client: Any) -> None:
        handler = self._handler(redis_client)
        body_json = {
            "subscription": {
                "type": "stream.offline",
                "condition": {"broadcaster_user_id": "999"},
            },
            "event": {"broadcaster_user_id": "999", "broadcaster_user_login": "waddlebot"},
        }
        body = json.dumps(body_json).encode()
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        headers[EVENTSUB_MESSAGE_TYPE] = "notification"

        response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json=body_json
        )
        assert status == 200
        assert response == {"status": "ok"}

        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        raw = await redis_client.rpop(ingest_key)
        assert raw is not None
        event = json.loads(raw)
        assert event["event_type"] == "stream.offline"
        assert event["broadcaster_id"] == "999"
        assert event["metadata"] == {}

    async def test_notification_ignores_an_unknown_event_type(self, redis_client: Any) -> None:
        handler = self._handler(redis_client)
        body_json = {
            "subscription": {"type": "channel.update", "condition": {}},
            "event": {"broadcaster_user_id": "999"},
        }
        body = json.dumps(body_json).encode()
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        headers[EVENTSUB_MESSAGE_TYPE] = "notification"

        response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json=body_json
        )
        assert status == 200
        assert response == {"status": "ignored"}

        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        assert await redis_client.rpop(ingest_key) is None

    async def test_revocation_is_acknowledged(self, redis_client: Any) -> None:
        handler = self._handler(redis_client)
        body_json = {"subscription": {"type": "channel.follow", "status": "authorization_revoked"}}
        body = json.dumps(body_json).encode()
        headers = _signed_headers(message_id="m1", timestamp="t1", body=body)
        headers[EVENTSUB_MESSAGE_TYPE] = "revocation"

        response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json=body_json
        )
        assert status == 200
        assert response == {"status": "acknowledged"}
