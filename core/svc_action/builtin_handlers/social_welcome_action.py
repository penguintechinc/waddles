"""Social welcome action handler -- sends welcome message to the original channel.

Receives a welcome message from the process stage and sends it as a reply
using the platform's native send-message API. Handles retryable and
non-retryable errors per the v3 transport contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request


async def send_welcome(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send a welcome message to the channel where the first message arrived.

    Resolves channel ID from the event (reply-in-place, primary) with a
    config-level fallback for proactive sends. Requires a platform-specific
    token ref to be configured; token is resolved from the environment at
    dispatch time via waddle_transports.signing.resolve_secret.

    Raises NonRetryableTransportError for a config/auth failure and
    RetryableTransportError for rate-limit/5xx/network errors. The runner
    owns all retry/backoff timing; this handler only classifies the failure.

    Args:
        envelope: StageEnvelope containing the event and audit context.
        config: Handler configuration (channel_id fallback, token_ref, etc.).
        http_client: AsyncClient for outbound requests, SSRF-guarded by runner.

    Returns:
        TransportResult with transport="bundle", detail, and http_status.

    """
    event_payload = envelope.event.payload
    payload_channel_id = event_payload.get("channel_id")
    config_channel_id = config.get("channel_id")

    # Reply-in-place (primary): use channel from the event payload
    channel_id = (
        payload_channel_id
        if isinstance(payload_channel_id, str) and payload_channel_id
        else (
            config_channel_id if isinstance(config_channel_id, str) and config_channel_id else None
        )
    )
    if channel_id is None:
        raise NonRetryableTransportError(
            "welcome bundle could not resolve channel_id from either "
            "envelope.event.payload['channel_id'] (reply-in-place) or "
            "config['channel_id'] (fallback)"
        )

    # Extract message text
    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError("action envelope event.payload missing required 'text'")

    # FLAG: Platform-specific token ref and API base are hardcoded as placeholders.
    # In production, these should come from config (set at activation time).
    # Example: config["api_token_ref"] = "DISCORD_BOT_TOKEN", config["api_base"] = "https://discord.com/api/v10"
    # For now, use Discord as the exemplar platform.
    token_ref = config.get("api_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError("welcome bundle config missing 'api_token_ref'")

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"welcome token resolution failed: {exc}") from exc

    # FLAG: API endpoint is hardcoded for exemplar; should be config-driven.
    api_base = config.get("api_base", "https://discord.com/api/v10")
    url = f"{api_base}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    body = {"content": text}

    # Make the API request with SSRF guard
    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"welcome API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"welcome API request failed: {exc}") from exc

    # Classify HTTP response
    if response.status_code == 429:
        raise RetryableTransportError("welcome API rate limited", http_status=429)
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"welcome API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"welcome API returned client error: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"welcome API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"welcome message sent to channel {channel_id}",
        http_status=response.status_code,
    )
