"""Social alias ACTION handler -- sends alias responses back to Discord/Twitch.

Ported from action/pushing/<platform>_action_module. Takes an expanded alias
message from the process stage and sends it to the target platform via the
appropriate API endpoint. Reuses waddle_transports' SSRF guard and secret
resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send expanded alias message back to Discord/Twitch.

    Reply-in-place: sends `envelope.event.payload["text"]` (the expanded alias)
    to the channel from `envelope.event.payload["channel_id"]` or fallback to
    `config["channel_id"]`. Supports both Discord and Twitch via the platform
    field in the envelope.

    Raises NonRetryableTransportError for config/auth failures and
    RetryableTransportError for rate-limits/5xx/network errors.
    """
    platform = envelope.event.platform
    if platform not in ("discord", "twitch"):
        raise NonRetryableTransportError(
            f"social.alias bundle does not support platform: {platform}"
        )

    if platform == "discord":
        return await _send_discord(envelope, config, http_client=http_client)

    return await _send_twitch(envelope, config, http_client=http_client)


async def _send_discord(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send to Discord via REST API."""
    event_payload = envelope.event.payload
    payload_channel_id = event_payload.get("channel_id")
    config_channel_id = config.get("channel_id")
    channel_id = (
        payload_channel_id
        if isinstance(payload_channel_id, str) and payload_channel_id
        else (
            config_channel_id if isinstance(config_channel_id, str) and config_channel_id else None
        )
    )
    if channel_id is None:
        raise NonRetryableTransportError(
            "social.alias bundle could not resolve a channel_id from either "
            "envelope.event.payload['channel_id'] or config['channel_id']"
        )

    token_ref = config.get("bot_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError(
            "social.alias bundle config missing required 'bot_token_ref'"
        )

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"bot token resolution failed: {exc}") from exc

    api_base = config.get("api_base", "https://discord.com/api/v10")
    url = f"{api_base}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    body = {"content": text}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"Discord API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"Discord API request failed: {exc}") from exc

    if response.status_code == 429:
        raise RetryableTransportError("Discord API rate limited", http_status=429)
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"Discord API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"Discord API returned client error: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"Discord API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"alias message sent to Discord, channel={channel_id}",
        http_status=response.status_code,
    )


async def _send_twitch(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send to Twitch via Chat API."""
    event_payload = envelope.event.payload
    payload_channel_name = event_payload.get("channel_name")
    config_channel_name = config.get("channel_name")
    channel_name = (
        payload_channel_name
        if isinstance(payload_channel_name, str) and payload_channel_name
        else (
            config_channel_name
            if isinstance(config_channel_name, str) and config_channel_name
            else None
        )
    )
    if channel_name is None:
        raise NonRetryableTransportError(
            "social.alias bundle could not resolve a channel_name from either "
            "envelope.event.payload['channel_name'] or config['channel_name']"
        )

    token_ref = config.get("bot_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError(
            "social.alias bundle config missing required 'bot_token_ref'"
        )

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"bot token resolution failed: {exc}") from exc

    api_base = config.get("api_base", "https://api.twitch.tv/helix")
    url = f"{api_base}/chat/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Client-ID": config.get("client_id", ""),
        "Content-Type": "application/json",
    }
    body = {"broadcaster_id": config.get("broadcaster_id", ""), "message": text}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"Twitch API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"Twitch API request failed: {exc}") from exc

    if response.status_code == 429:
        raise RetryableTransportError("Twitch API rate limited", http_status=429)
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"Twitch API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"Twitch API returned client error: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"Twitch API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"alias message sent to Twitch, channel={channel_name}",
        http_status=response.status_code,
    )
