"""Twitch EventSub webhook verification + normalization -- `webhook_push` ingest.

Real HMAC-SHA256 signature verification, ported from `trigger/receiver/
twitch_module/services/eventsub_handler.py`'s own `_verify_signature`
(byte-identical algorithm: `sha256=` + hex HMAC of `message_id +
timestamp + body` under the EventSub secret) rather than reimplementing
it -- per this connector's own task spec. Deliberately does NOT port that
legacy module's own duplicate-message-id in-memory cache (`_processed_
ids`) or its `subscribe_to_events`/`_create_subscription` subscription-
management calls -- both are out of scope for this MVP (subscription
management is a one-time setup operation, not part of the inbound webhook
path this module owns).

Mounted at `POST /eventsub/twitch/webhook` (`app.py`) -- unlike the IRC
gateway receiver (`receivers/twitch_irc.py`), EventSub genuinely IS a
webhook Twitch calls into this service (`communication_model=
"webhook_push"`, `builtin_handlers/twitch_gateway_manifest.py`'s own
`TWITCH_EVENTSUB_MANIFEST`), so this module fans a normalized event out
via the same `fanout.fan_out_event` machinery the IRC receiver uses,
rather than anything IRC/socket-shaped.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from flask_core.app_registry import AppRegistry

from fanout import RedisLike, fan_out_event

logger = logging.getLogger(__name__)

EVENTSUB_MESSAGE_TYPE = "Twitch-Eventsub-Message-Type"
EVENTSUB_SIGNATURE = "Twitch-Eventsub-Message-Signature"
EVENTSUB_TIMESTAMP = "Twitch-Eventsub-Message-Timestamp"
EVENTSUB_MESSAGE_ID = "Twitch-Eventsub-Message-Id"

#: The `consumes` tag the EventSub ingest bundle declares
#: (`builtin_handlers/twitch_gateway_manifest.py`'s `TWITCH_EVENTSUB_MANIFEST`).
CONSUMES_TAG = "twitch.eventsub"

#: EventSub subscription types this connector's MVP normalizes -- matches
#: `builtin_handlers/twitch_eventsub_ingest.py`'s own `KNOWN_EVENT_TYPES`.
#:
#: gh #287 S10 (live ON/OFF detection): `stream.online`/`stream.offline`
#: added -- this is the webhook-side companion edit
#: `builtin_handlers/twitch_eventsub_ingest.py`'s own module docstring flagged as
#: the "known gap" blocking a real Twitch webhook delivery for either type
#: from ever reaching `normalize()` (this handler's `handle_webhook`
#: drops any `event_type` not in this set BEFORE `build_raw_event` runs).
#: Both subscribe the same way `channel.raid` already does -- EventSub
#: `version="1"`, `condition={"broadcaster_user_id": <id>}`, no extra
#: condition field (unlike `channel.follow`, which additionally needs
#: `moderator_user_id`) -- per the legacy module's own
#: `subscribe_to_events`/`_create_subscription` default list and
#: condition-building logic (`trigger/receiver/twitch_module/services/
#: eventsub_handler.py`), which this handler does not itself port (see
#: this module's own docstring -- subscription *creation* stays out of
#: scope, only the inbound webhook filter is this set's job).
DEFAULT_SUBSCRIPTION_TYPES = frozenset(
    {
        "channel.follow",
        "channel.subscribe",
        "channel.subscription.gift",
        "channel.cheer",
        "channel.raid",
        "stream.online",
        "stream.offline",
    }
)


def verify_signature(*, secret: str, headers: dict[str, str], body: bytes) -> bool:
    """Verify the HMAC-SHA256 EventSub signature. Byte-identical to the legacy module's own.

    `hmac.compare_digest` (constant-time) rather than `==` -- a naive
    string comparison is a timing side channel (security.md: no
    reimplemented equivalents that regress on a documented control).
    """
    signature = headers.get(EVENTSUB_SIGNATURE, "")
    timestamp = headers.get(EVENTSUB_TIMESTAMP, "")
    message_id = headers.get(EVENTSUB_MESSAGE_ID, "")
    if not all([signature, timestamp, message_id]):
        return False

    message = message_id.encode() + timestamp.encode() + body
    expected = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def build_raw_event(
    event_type: str, event: dict[str, Any], subscription: dict[str, Any]
) -> dict[str, Any]:
    """Build the raw event dict fanned out to `builtin_handlers.twitch_eventsub_ingest:normalize`.

    Field set ported from the legacy module's own `_build_event_data`,
    trimmed to what `builtin_handlers/twitch_eventsub_ingest.py`'s `normalize()`
    actually reads -- per-event-type metadata (tier/bits/viewers/etc.)
    is folded into `metadata`, matching that module's own shape.
    """
    broadcaster_id = event.get("broadcaster_user_id") or subscription.get("condition", {}).get(
        "broadcaster_user_id", ""
    )
    metadata: dict[str, Any] = {}
    if event_type == "channel.subscribe":
        metadata = {"tier": event.get("tier", "1000"), "is_gift": event.get("is_gift", False)}
    elif event_type == "channel.subscription.gift":
        metadata = {
            "tier": event.get("tier", "1000"),
            "total": event.get("total", 1),
            "is_anonymous": event.get("is_anonymous", False),
        }
    elif event_type == "channel.raid":
        metadata = {"viewers": event.get("viewers", 0)}
    elif event_type == "channel.cheer":
        metadata = {"bits": event.get("bits", 0), "is_anonymous": event.get("is_anonymous", False)}
    elif event_type == "stream.online":
        # gh #287 S10 -- real Twitch payload: {id, broadcaster_user_id,
        # broadcaster_user_login, broadcaster_user_name, type, started_at}.
        # `type` ("live"/"playlist"/"watch_party"/"premiere"/"rerun") and
        # `started_at` ride in `metadata` exactly like `channel.raid`'s
        # `viewers` above -- `builtin_handlers/twitch_eventsub_ingest.py::normalize`
        # passes `metadata` straight through onto `PlatformEvent.payload`.
        metadata = {"type": event.get("type", "live"), "started_at": event.get("started_at", "")}
    elif event_type == "stream.offline":
        # Real Twitch payload carries no extra fields beyond
        # broadcaster_user_id/_login/_name -- metadata is deliberately
        # empty, matching `test_normalizes_a_stream_offline_event`'s own
        # assertion in `builtin_handlers/twitch_eventsub_ingest.py`'s test suite.
        metadata = {}

    return {
        "platform": "twitch",
        "event_type": event_type,
        "broadcaster_id": broadcaster_id,
        "broadcaster_login": event.get("broadcaster_user_login"),
        "user_id": event.get("user_id") or event.get("from_broadcaster_user_id"),
        "user_login": event.get("user_login") or event.get("from_broadcaster_user_login"),
        "user_display_name": event.get("user_name") or event.get("from_broadcaster_user_name"),
        "metadata": metadata,
    }


class TwitchEventSubHandler:
    """Verifies + fans out real Twitch EventSub webhook notifications."""

    def __init__(
        self, *, secret: str, redis_client: RedisLike, registry: AppRegistry, tenant_slug: str
    ) -> None:
        """Store the EventSub secret + fan-out dependencies. Stateless otherwise."""
        self._secret = secret
        self._redis = redis_client
        self._registry = registry
        self._tenant_slug = tenant_slug

    async def handle_webhook(
        self, *, headers: dict[str, str], body: bytes, body_json: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """Verify + route one EventSub webhook POST. Returns `(response_body, http_status)`.

        `webhook_callback_verification` (subscription setup handshake)
        echoes the challenge back with 200; `notification` fans a
        normalized event out and acks 200; an unrecognized message type
        or a bad signature never fans anything out.
        """
        if not verify_signature(secret=self._secret, headers=headers, body=body):
            logger.warning("eventsub.invalid_signature")
            return {"error": "invalid signature"}, 403

        message_type = headers.get(EVENTSUB_MESSAGE_TYPE, "")
        if message_type == "webhook_callback_verification":
            challenge = body_json.get("challenge", "")
            logger.info("eventsub.subscription_verified")
            return {"challenge": challenge}, 200

        if message_type == "notification":
            subscription = body_json.get("subscription", {})
            event = body_json.get("event", {})
            event_type = subscription.get("type", "")
            if event_type not in DEFAULT_SUBSCRIPTION_TYPES:
                logger.debug("eventsub.unhandled_event_type type=%s", event_type)
                return {"status": "ignored"}, 200

            raw_event = build_raw_event(event_type, event, subscription)
            try:
                count = await fan_out_event(
                    raw_event,
                    consumes_tag=CONSUMES_TAG,
                    tenant=self._tenant_slug,
                    # T9 (2026-09-03 coordination): fan out at community=None
                    # for the demo -- see receivers/twitch_irc.py's own
                    # docstring for the same change applied to chat events.
                    community=None,
                    redis_client=self._redis,
                    registry=self._registry,
                )
                logger.debug("eventsub.fanned count=%s type=%s", count, event_type)
            except Exception as exc:  # noqa: BLE001 - one bad event must never 500 the webhook
                logger.error("eventsub.fanout_failed error=%s", exc)
            return {"status": "ok"}, 200

        if message_type == "revocation":
            subscription = body_json.get("subscription", {})
            logger.warning(
                "eventsub.subscription_revoked type=%s status=%s",
                subscription.get("type"),
                subscription.get("status"),
            )
            return {"status": "acknowledged"}, 200

        return {"status": "unknown_type"}, 200
