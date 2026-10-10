"""Marketing engagement send-message ACTION handler -- send engagement notifications.

Ported from `core/engagement_module/app.py` (v2). Sends engagement-related
notifications (poll announcements, form confirmations) to platform channels
via HTTP API call, SSRF-guarded.

This handler formats engagement data and sends it to the resolved channel.
Reply-in-place semantics apply: `channel_id` comes from the triggering
event's payload (primary), with `config["channel_id"]` as fallback for
proactive sends.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request


async def send_engagement_notification(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send engagement notification (poll/form) to the resolved channel.

    Formats and sends an engagement notification to a platform channel.
    Reply-in-place: `channel_id` comes from `envelope.event.payload` (primary)
    or `config` (fallback). Text content comes from `envelope.event.payload
    ["text"]`, optionally augmented with engagement-specific fields (poll
    options, form fields, submission count, etc.).

    Raises `NonRetryableTransportError` for config/auth failures (no resolvable
    channel_id, unresolvable secret, 401/403, any other 4xx) and
    `RetryableTransportError` for 429 (rate limited), 5xx, or network errors.
    """
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
            "engagement bundle could not resolve a channel_id from either "
            "envelope.event.payload['channel_id'] (reply-in-place) or "
            "config['channel_id'] (fallback)"
        )

    notification_token_ref = config.get("notification_token_ref")
    if not isinstance(notification_token_ref, str) or not notification_token_ref:
        raise NonRetryableTransportError(
            "engagement bundle config missing required 'notification_token_ref'"
        )

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    try:
        token = resolve_secret(notification_token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"engagement token resolution failed: {exc}") from exc

    # Build notification body with engagement-specific fields if present
    notification_body: dict[str, Any] = {"text": text}
    if event_payload.get("poll_id"):
        notification_body["poll_id"] = event_payload["poll_id"]
    if event_payload.get("form_id"):
        notification_body["form_id"] = event_payload["form_id"]
    if event_payload.get("options"):
        notification_body["options"] = event_payload["options"]
    if event_payload.get("event_type"):
        notification_body["engagement_type"] = event_payload["event_type"]

    api_base = config.get("api_base", "https://api.example/v1")
    url = f"{api_base}/channels/{channel_id}/notifications"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = await guarded_request(
            http_client,
            "POST",
            url,
            headers=headers,
            json=notification_body,
        )
    except SSRFError as exc:
        raise NonRetryableTransportError(
            f"engagement API URL rejected by SSRF guard: {exc}"
        ) from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"engagement API request failed: {exc}") from exc

    if response.status_code == 429:
        raise RetryableTransportError(
            "engagement API rate limited",
            http_status=429,
        )
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"engagement API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"engagement API returned client error: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"engagement API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"engagement notification sent, channel={channel_id}",
        http_status=response.status_code,
    )
