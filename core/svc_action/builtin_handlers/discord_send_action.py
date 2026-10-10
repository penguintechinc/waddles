"""Discord send-message ACTION handler -- real Discord Bot API `POST .../messages` call.

Ported from `action/pushing/discord_action_module/services/
discord_service.py`'s `send_message` (the legacy module's real Discord API
logic -- Bot-token auth, Create Message endpoint, 429/`Retry-After`
handling) into the built-in action-stage handler contract:
`async def <name>(envelope, config, *, http_client) -> TransportResult`
(`runner.py`).

Reuses the shared `waddle_transports` library's SSRF guard
(`waddle_transports.url_guard.guarded_request`) and secret resolution
(`waddle_transports.signing.resolve_secret` -- an env-var-name
indirection, never a raw bot token in `app_catalog`/`app_activations`
config) rather than reimplementing either. Deliberately calls
`guarded_request` **directly** rather than routing through `waddle_
transports`' `http` transport's `rest_api` sub_type -- that generic
sub_type treats every 4xx except 401/403 as non-retryable, which would
misclassify Discord's 429 rate-limit response as a permanent failure;
Discord's real semantics need this handler's own status-code
interpretation, exactly like `waddle_transports/transports/http.py`'s own
module docstring documents for its `grpc` sub_type's proto-stub boundary.
Also deliberately does **not** port `discord_service.py`'s own
`asyncio.sleep`-based 429 retry loop, per-endpoint rate-limit dict, or its
`discord_actions` audit table -- all three are now handled by svc-action's
platform-level `retry_with_backoff` (`RetryableTransportError` on
429/5xx, `runner.py::_handle_envelope`) and `action_dispatch_log`
respectively, so this handler stays a thin, stateless script instead of
duplicating retry/audit infrastructure the platform already provides.
Seeded via `config/postgres/migrations/082_discord_send_action_bundle.sql`
as `waddles.bot.discord.default`'s `stages.action.entrypoint`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request

#: Real Discord REST API base -- overridable via handler `config["api_base"]`
#: (tests point this at a literal-IP mock target, matching every other
#: transport's test convention of avoiding real DNS resolution in unit
#: tests -- `waddle_transports.url_guard.validate_url` resolves the host
#: via `socket.getaddrinfo` before every request, including in tests).
_DEFAULT_API_BASE = "https://discord.com/api/v10"


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply in-place: send `envelope.event.payload["text"]` to the resolved `channel_id`.

    "Reply-in-place" is the primary behavior -- `channel_id` comes from
    the triggering event's own payload (the channel the inbound message
    that caused this action came from), not a statically configured
    channel, so a handler activated once serves every channel the bot
    is in rather than always posting to one hardcoded channel. `config`
    (the handler's resolved `stages.action.config`, resolved by hub-api's
    distribution endpoint and handed to this entrypoint by `runner.py::
    _handle_envelope` via the poller) supplies `channel_id` only as a
    fallback for a proactive/scheduled send with no originating channel
    in its payload, and must always declare `bot_token_ref` (an env-var
    *name*, resolved via `resolve_secret` -- never a literal token in DB
    config). `api_base` optionally overrides the Discord API root
    (default: the real Discord API).

    Raises `NonRetryableTransportError` for a config/auth failure (no
    resolvable channel_id, unresolvable secret, 401/403, any other 4xx)
    and `RetryableTransportError` for a 429 (rate limited --
    `retry_with_backoff` in `runner.py` owns the actual backoff; this
    handler never sleeps itself) or a 5xx/network error.
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
            "discord bundle could not resolve a channel_id from either "
            "envelope.event.payload['channel_id'] (reply-in-place) or "
            "config['channel_id'] (fallback)"
        )

    bot_token_ref = config.get("bot_token_ref")
    if not isinstance(bot_token_ref, str) or not bot_token_ref:
        raise NonRetryableTransportError("discord bundle config missing required 'bot_token_ref'")

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    try:
        bot_token = resolve_secret(bot_token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"discord bot token resolution failed: {exc}") from exc

    api_base = config.get("api_base", _DEFAULT_API_BASE)
    url = f"{api_base}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {"content": text}
    embed = event_payload.get("embed")
    if isinstance(embed, dict):
        body["embeds"] = [embed]

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"discord API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"discord API request failed: {exc}") from exc

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "1")
        raise RetryableTransportError(
            f"discord API rate limited, retry after {retry_after}s", http_status=429
        )
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"discord API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"discord API returned client error: HTTP {response.status_code} {response.text[:200]}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"discord API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    message_id = None
    try:
        message_id = response.json().get("id")
    except Exception:  # noqa: BLE001, S110 -- best-effort audit detail only, never fatal
        pass  # nosec B110 -- same rationale as the noqa above, bandit's own suppression syntax

    return TransportResult(
        transport="bundle",
        detail=f"discord message sent, channel={channel_id} message_id={message_id}",
        http_status=response.status_code,
    )
