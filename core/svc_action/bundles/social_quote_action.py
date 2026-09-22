"""Social quote action bundle -- handles quote write operations and sends replies.

Action-stage bundle that receives quote add/get/random intent from process stage,
executes any database writes, and sends the final reply text to Discord/Twitch.
Follows the same reply-in-place pattern as discord_send_action.py and
twitch_send_action.py: payload's channel_id/channel_name takes precedence over
config fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from flask_core import StageEnvelope, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_write
from waddle_transports import NonRetryableTransportError, TransportResult
from waddle_transports.transports.irc_relay import RelayOutboundIrcTransport

logger = logging.getLogger(__name__)

#: Lazily-built, process-wide Valkey client for IRC relay (same pattern as twitch_send_action.py)
_redis_client: Any | None = None


def _get_redis_client(config: Mapping[str, Any]) -> Any:
    """Build (once) or return the cached Valkey client for the outbound IRC relay."""
    global _redis_client
    if _redis_client is None:
        import os

        import redis.asyncio as redis

        url = (
            os.environ.get("VALKEY_URL")
            or os.environ.get("REDIS_URL")
            or "redis://localhost:6379/0"
        )
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Execute quote action and reply in-place.

    Handles quote add/get/random intents from process stage, executes any
    database writes, and sends the reply text to the origin channel or
    configured fallback. `channel` resolution: `envelope.event.payload[
    "channel_name"]` (Twitch) or `["channel_id"]` (Discord) first, falling
    back to config's own `channel`/`channel_id`.

    Raises `NonRetryableTransportError` for a config error or empty event
    payload `text`; propagates any `RetryableTransportError` from the relay
    transport unchanged.
    """
    event_payload = envelope.event.payload
    text = event_payload.get("text")

    # Handle quote add action
    if event_payload.get("_quote_action") == "add":
        quote_text = event_payload.get("_quote_text")
        actor = event_payload.get("_actor")
        if quote_text:
            try:
                result = await _add_quote_to_db(quote_text, actor)
                if result:
                    text = f"Quote added! (ID: {result})"
                else:
                    text = "Failed to add quote."
            except Exception:
                text = "Error adding quote."
        else:
            text = "Could not add quote (missing data)."

    # Resolve channel for reply
    payload_channel_id = event_payload.get("channel_id")
    payload_channel_name = event_payload.get("channel_name")

    # Determine platform and get channel
    platform = envelope.event.platform.lower() if envelope.event.platform else "discord"

    if platform == "twitch":
        # Twitch uses channel_name
        channel = payload_channel_name if isinstance(payload_channel_name, str) else None
        if not channel:
            channel = config.get("channel")
    else:
        # Discord uses channel_id
        channel = payload_channel_id if isinstance(payload_channel_id, str) else None
        if not channel:
            channel = config.get("channel_id")

    channel = channel if isinstance(channel, str) and channel else None

    if not channel:
        raise NonRetryableTransportError(
            "social quote bundle could not resolve a channel from either "
            "envelope.event.payload['channel_id'/'channel_name'] (reply-in-place) or "
            "config['channel'/'channel_id'] (fallback)"
        )

    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    # Send via platform-specific transport
    if platform == "twitch":
        transport = RelayOutboundIrcTransport(
            provider="twitch", redis_client=_get_redis_client(config)
        )
        return await transport.send({"channel": channel}, {"text": text})

    # Discord via guarded_request
    from waddle_transports.signing import SecretResolutionError, resolve_secret
    from waddle_transports.url_guard import SSRFError, guarded_request

    token_ref = config.get("bot_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError(
            "social quote bundle config missing required 'bot_token_ref'"
        )

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"discord token resolution failed: {exc}") from exc

    api_base = config.get("api_base", "https://discord.com/api/v10")
    url = f"{api_base}/channels/{channel}/messages"
    # Discord's Bot API requires the `Bot` auth scheme, never `Bearer` (that
    # scheme is for OAuth2 user access tokens) -- a valid bot token sent as
    # `Bearer` is rejected with 401 even though the token itself is fine.
    # Matches discord_send_action.py:send_message's own header.
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    body = {"content": text}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"discord API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        from waddle_transports import RetryableTransportError

        raise RetryableTransportError(f"discord API request failed: {exc}") from exc

    if response.status_code == 429:
        from waddle_transports import RetryableTransportError

        raise RetryableTransportError("discord API rate limited", http_status=429)
    if response.status_code in (401, 403):
        logger.warning(
            "social_quote_action.discord_send_rejected community_id=%s bot_token_ref=%s "
            "channel=%s status=%s",
            envelope.community,
            token_ref,
            channel,
            response.status_code,
        )
        raise NonRetryableTransportError(
            f"discord API rejected auth for bot_token_ref={token_ref!r}: "
            f"HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"discord API returned client error: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        from waddle_transports import RetryableTransportError

        raise RetryableTransportError(
            f"discord API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"quote message sent, channel={channel}",
        http_status=response.status_code,
    )


async def _add_quote_to_db(quote_text: str, actor: str | None) -> int | None:
    """Add a quote to the database and return the quote ID, or None on failure."""
    try:
        dal = get_bundle_dal()
        now = datetime.now(UTC).isoformat()
        result = await raw_sql_write(
            dal,
            "INSERT INTO quotes (quote_text, quoted_username, is_approved, created_at, updated_at) "
            "VALUES (:quote_text, :quoted_username, TRUE, :created_at, :updated_at) RETURNING id",
            {
                "quote_text": quote_text,
                "quoted_username": actor or "unknown",
                "created_at": now,
                "updated_at": now,
            },
        )
        if not result:
            return None
        raw_id = result[0].get("id")
        return raw_id if isinstance(raw_id, int) else None
    except Exception:
        return None
