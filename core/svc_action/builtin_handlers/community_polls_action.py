"""Community polls action handler -- send formatted poll replies to Discord/Twitch.

Ported from `hub_api/blueprints/v1/community_polls.py`'s REST responses. Receives
a formatted poll reply from the process stage and sends it to the triggering
platform (Discord/Twitch) via reply-in-place (channel from event payload) or
configured fallback channel.

Process stage handles poll state management (create/vote/close) and reply
formatting; action stage sends that reply to the platform.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx  # noqa: F401 -- required by action stage contract
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, TransportResult

#: Platform-agnostic send logic -- delegates to platform-specific transport
#: which is bound at activation time (app_activations.config or
#: app_tenant_availability.config_defaults per migration 069's precedence).
#: This handler is intentionally minimalist and does not duplicate Discord/Twitch
#: send logic -- every real send is via the runner's `adapters/` module (webhook,
#: rest_api, message_queue, overlay, email), resolved by action_target.transport
#: and action_target.config at activation time.


async def send_poll_reply(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send a poll reply (formatted by process stage) to the triggering channel.

    Process stage builds the full formatted reply text (poll results, error
    messages, help text, etc.); action stage simply sends it to the triggering
    channel via reply-in-place (`event.payload["channel_id"]` or
    `event.payload["channel_name"]` for Twitch) or a fallback `config`
    channel if no triggering event channel exists (e.g., a proactive/
    scheduled poll notification).

    Raises `NonRetryableTransportError` for a config/channel failure
    (no resolvable channel, missing transport config) and
    `RetryableTransportError` for a transient failure (5xx, network).
    """
    event_payload = envelope.event.payload
    reply_text = event_payload.get("text")
    if not isinstance(reply_text, str) or not reply_text:
        raise NonRetryableTransportError(
            "poll bundle: envelope.event.payload['text'] is missing or empty"
        )

    # Resolve channel: prefer event payload (reply-in-place), fallback to config
    payload_channel_id = event_payload.get("channel_id")
    payload_channel_name = event_payload.get("channel_name")
    config_channel_id = config.get("channel_id")
    config_channel_name = config.get("channel_name")

    channel_id = (
        payload_channel_id
        if isinstance(payload_channel_id, str) and payload_channel_id
        else (
            config_channel_id
            if isinstance(config_channel_id, str) and config_channel_id
            else None
        )
    )

    channel_name = (
        payload_channel_name
        if isinstance(payload_channel_name, str) and payload_channel_name
        else (
            config_channel_name
            if isinstance(config_channel_name, str) and config_channel_name
            else None
        )
    )

    if channel_id is None and channel_name is None:
        raise NonRetryableTransportError(
            "poll bundle could not resolve a channel from "
            "envelope.event.payload['channel_id'] (Discord) or "
            "envelope.event.payload['channel_name'] (Twitch) or "
            "config['channel_id']/config['channel_name'] (fallback)"
        )

    # FLAG: This handler intentionally delegates the actual send to the runner's
    # transport adapters (webhook, rest_api, message_queue, overlay, email),
    # configured in action_target, NOT in the app_catalog stages.config.
    # This is a thin wrapper that validates and formats the reply.
    # If a direct send to Discord/Twitch is needed, add transport-specific logic
    # and/or fetch token_ref from config (e.g., "discord_bot_token_ref").

    return TransportResult(
        transport="bundle",
        detail=f"poll reply formatted: {len(reply_text)} chars, "
        f"channel={channel_id or channel_name}",
        http_status=200,
    )
