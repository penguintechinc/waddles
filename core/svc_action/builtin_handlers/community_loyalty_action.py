"""Community loyalty action handler -- calls hub-api's internal Community Loyalty routes, replies.

Action-stage handler for `!points`/`!top`/`!shop`/`!redeem` (process stage:
`builtin_handlers.community_loyalty_process`, cross-app routed here per gh #298's
`PROCESS_TARGET_APP_ID_KEY` mechanism -- see that handler's docstring).
Calls hub-api's service-key-gated internal Community Loyalty endpoints
(`hub_api/blueprints/v1/community_loyalty.py`'s `loyalty_internal_bp`)
-- never the JWT/admin-scoped `/api/v1/admin/<id>/loyalty/*` routes,
since the requester is a viewer typing a chat command, not an
authenticated hub-api user. Auth mirrors `builtin_handlers.social_music_action`'s
own `X-Service-Key`/`HUB_API_URL` calling convention.

Entrypoint `loyalty` dispatches on `event.payload["subcommand"]`
(`"balance"|"top"|"shop"|"redeem"|"adjust"`, the process stage's exact
outgoing shape -- see that module's docstring) to one of five private
helpers, each returning `(chat reply text, structured data dict or None)`
and never raising -- same graceful-degradation contract every sibling
action handler uses (`social_music_action.py::_enqueue`/`_check_status`):
an unreachable hub-api, a 5xx, or a malformed 2xx body all fold into the
generic `loyalty unavailable (hub-api error <code>|unreachable)` reply
rather than propagating. A 4xx response (including `!redeem`'s documented
409s -- `not enough points`/`item out of stock`/`unknown item`/`loyalty
is disabled here`) relays hub-api's `error.message` verbatim, same
convention as `social_music_action.py::_set_policy`/`_set_playback`.

`target` (view-another's-balance / `adjust`) is the raw login/id string
the process stage parsed from chat, unresolved -- this handler passes it
straight through as `platform_user_id` to hub-api's internal routes,
which are keyed on `(community_id, platform, platform_user_id)`
directly; there is no login->platform_user_id resolution step in this
MVP (`community_loyalty_process`'s own docstring).

`currency_name` defaults to the literal `"points"` wherever hub-api's
response omits it (the `adjust` endpoint's contract is `{balance}` only,
unlike `balance`/`top`'s `{..., currency_name, ...}`) -- keeps every
reply readable without a second round trip just to fetch a display label.

Reply-in-place: same channel-resolution (payload first, config fallback)
and Discord/Twitch dispatch as `builtin_handlers.social_music_action`/`builtin_handlers
.social_quote_action` -- each cross-app-routed feature action handler owns
its own outbound send, since routing goes to the FEATURE's `:action` key,
never the bot's own.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.transports.irc_relay import RelayOutboundIrcTransport

logger = logging.getLogger(__name__)

_BALANCE_PATH = "/api/v1/internal/loyalty/balance"
_LEADERBOARD_PATH = "/api/v1/internal/loyalty/leaderboard"
_ITEMS_PATH = "/api/v1/internal/loyalty/items"
_EARN_PATH = "/api/v1/internal/loyalty/earn"
_ADJUST_PATH = "/api/v1/internal/loyalty/adjust"
_REDEEM_PATH = "/api/v1/internal/loyalty/redeem"
_TIMEOUT_SECONDS = 5.0

_LEADERBOARD_LIMIT = 10

_DEFAULT_CURRENCY_NAME = "points"

_BALANCE_SUBCOMMAND = "balance"
_TOP_SUBCOMMAND = "top"
_SHOP_SUBCOMMAND = "shop"
_REDEEM_SUBCOMMAND = "redeem"
_ADJUST_SUBCOMMAND = "adjust"

_SHOP_EMPTY_REPLY = "the shop is empty"
_LEADERBOARD_EMPTY_SUFFIX = "(nobody has earned points yet)"
_REDEEM_PENDING_SUFFIX = " (awaiting mod approval)"

#: Lazily-built, process-wide Valkey client for IRC relay (same pattern as
#: `social_music_action.py`/`twitch_send_action.py`).
_redis_client: Any | None = None


def _get_redis_client(config: Mapping[str, Any]) -> Any:
    """Build (once) or return the cached Valkey client for the outbound IRC relay."""
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as redis

        url = (
            os.environ.get("VALKEY_URL")
            or os.environ.get("REDIS_URL")
            or "redis://localhost:6379/0"
        )
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


def _unavailable_reply(status_code: int | None) -> str:
    """Render the generic `loyalty unavailable` reply for a 5xx, timeout, or malformed response.

    `status_code=None` covers a network failure/timeout (no status code to
    report); otherwise the actual HTTP status is included.
    """
    if status_code is None:
        return "loyalty unavailable (hub-api unreachable)"
    return f"loyalty unavailable (hub-api error {status_code})"


def _relay_client_error(response: httpx.Response) -> str:
    """Extract hub-api's `error.message` from a 4xx body; falls back to the generic reply."""
    try:
        body = response.json()
        message = str((body.get("error") or {}).get("message", ""))
    except ValueError:
        message = ""
    return message or _unavailable_reply(response.status_code)


async def _get_balance(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    target_label: str | None,
) -> tuple[str, dict[str, object] | None]:
    """GET hub-api's internal balance endpoint; never raises.

    `target_label` is the ALREADY-normalized `target` typed in chat
    (`None` for the caller's own balance) -- used verbatim in the reply,
    never re-resolved.
    """
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")

    logger.debug(
        "community_loyalty_action.balance_request community_id=%s platform=%s", community_id,
        platform,
    )

    try:
        response = await http_client.get(
            f"{hub_api_base}{_BALANCE_PATH}",
            params={
                "community_id": community_id,
                "platform": platform,
                "platform_user_id": platform_user_id,
            },
            headers={"X-Service-Key": service_api_key},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "community_loyalty_action.balance_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(None), None)

    if 400 <= response.status_code < 500:
        return (_relay_client_error(response), None)
    if response.status_code >= 500:
        return (_unavailable_reply(response.status_code), None)

    try:
        data = response.json()["data"]
        balance = int(data["balance"])
        currency_name = str(data.get("currency_name", _DEFAULT_CURRENCY_NAME))
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "community_loyalty_action.balance_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(response.status_code), None)

    reply = (
        f"you have {balance} {currency_name}"
        if target_label is None
        else f"{target_label} has {balance} {currency_name}"
    )
    return (reply, {"balance": balance, "target": target_label})


async def _get_leaderboard(
    http_client: httpx.AsyncClient, *, community_id: int
) -> tuple[str, dict[str, object] | None]:
    """GET hub-api's internal leaderboard endpoint (top 10); never raises."""
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")

    logger.debug("community_loyalty_action.leaderboard_request community_id=%s", community_id)

    try:
        response = await http_client.get(
            f"{hub_api_base}{_LEADERBOARD_PATH}",
            params={"community_id": community_id, "limit": _LEADERBOARD_LIMIT},
            headers={"X-Service-Key": service_api_key},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "community_loyalty_action.leaderboard_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(None), None)

    if 400 <= response.status_code < 500:
        return (_relay_client_error(response), None)
    if response.status_code >= 500:
        return (_unavailable_reply(response.status_code), None)

    try:
        data = response.json()["data"]
        entries = data["entries"]
        currency_name = str(data.get("currency_name", _DEFAULT_CURRENCY_NAME))
        if not isinstance(entries, list):
            raise TypeError("'entries' is not a list")
        rendered: list[str] = []
        for index, entry in enumerate(entries[:_LEADERBOARD_LIMIT], start=1):
            name = entry.get("display_name") or entry["platform_user_id"]
            rendered.append(f"{index}. {name} — {int(entry['balance'])}")
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "community_loyalty_action.leaderboard_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(response.status_code), None)

    body = ", ".join(rendered) if rendered else _LEADERBOARD_EMPTY_SUFFIX
    reply = f"top {currency_name}: {body}"
    return (reply, {"entry_count": len(rendered)})


async def _get_shop(
    http_client: httpx.AsyncClient, *, community_id: int
) -> tuple[str, dict[str, object] | None]:
    """GET hub-api's internal items endpoint; never raises."""
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")

    logger.debug("community_loyalty_action.shop_request community_id=%s", community_id)

    try:
        response = await http_client.get(
            f"{hub_api_base}{_ITEMS_PATH}",
            params={"community_id": community_id},
            headers={"X-Service-Key": service_api_key},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "community_loyalty_action.shop_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(None), None)

    if 400 <= response.status_code < 500:
        return (_relay_client_error(response), None)
    if response.status_code >= 500:
        return (_unavailable_reply(response.status_code), None)

    try:
        items = response.json()["data"]["items"]
        if not isinstance(items, list):
            raise TypeError("'items' is not a list")
        rendered = []
        for item in items:
            if not item.get("enabled", True):
                continue
            sku = str(item["sku"])
            name = str(item["name"])
            cost = int(item["cost"])
            stock = item.get("stock")
            suffix = f" [x{int(stock)} left]" if stock is not None else ""
            rendered.append(f"{sku} — {name} ({cost}){suffix}")
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "community_loyalty_action.shop_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(response.status_code), None)

    reply = f"shop: {', '.join(rendered)}" if rendered else _SHOP_EMPTY_REPLY
    return (reply, {"item_count": len(rendered)})


async def _redeem(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    sku: str,
) -> tuple[str, dict[str, object] | None]:
    """POST hub-api's internal redeem endpoint; never raises.

    A 409 (`not enough points`/`item out of stock`/`unknown item`/
    `loyalty is disabled here`) relays hub-api's `error.message` verbatim
    via the shared `_relay_client_error` 4xx path -- task requirement.
    """
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")
    body = {
        "community_id": community_id,
        "platform": platform,
        "platform_user_id": platform_user_id,
        "sku": sku,
    }

    logger.debug(
        "community_loyalty_action.redeem_request community_id=%s sku=%s", community_id, sku
    )

    try:
        response = await http_client.post(
            f"{hub_api_base}{_REDEEM_PATH}",
            json=body,
            headers={"X-Service-Key": service_api_key},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "community_loyalty_action.redeem_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(None), None)

    if 400 <= response.status_code < 500:
        logger.warning(
            "community_loyalty_action.redeem_rejected community_id=%s sku=%s status=%s",
            community_id,
            sku,
            response.status_code,
        )
        return (_relay_client_error(response), None)
    if response.status_code >= 500:
        return (_unavailable_reply(response.status_code), None)

    try:
        data = response.json()["data"]
        item_name = str(data["item_name"])
        cost = int(data["cost"])
        balance = int(data["balance"])
        status = str(data.get("status", "fulfilled"))
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "community_loyalty_action.redeem_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(response.status_code), None)

    reply = f"redeemed {item_name} for {cost} — {balance} left"
    if status == "pending":
        reply += _REDEEM_PENDING_SUFFIX
    return (reply, {"item_name": item_name, "cost": cost, "balance": balance, "status": status})


async def _adjust(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    delta: int,
    actor_platform_user_id: str | None,
    target_label: str,
) -> tuple[str, dict[str, object] | None]:
    """POST hub-api's internal adjust endpoint (`!points add|remove`); never raises."""
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")
    body = {
        "community_id": community_id,
        "platform": platform,
        "platform_user_id": platform_user_id,
        "delta": delta,
        "actor_platform_user_id": actor_platform_user_id,
        "note": None,
    }

    logger.debug(
        "community_loyalty_action.adjust_request community_id=%s delta=%d", community_id, delta
    )

    try:
        response = await http_client.post(
            f"{hub_api_base}{_ADJUST_PATH}",
            json=body,
            headers={"X-Service-Key": service_api_key},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "community_loyalty_action.adjust_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(None), None)

    if 400 <= response.status_code < 500:
        return (_relay_client_error(response), None)
    if response.status_code >= 500:
        return (_unavailable_reply(response.status_code), None)

    try:
        data = response.json()["data"]
        balance = int(data["balance"])
        currency_name = str(data.get("currency_name", _DEFAULT_CURRENCY_NAME))
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "community_loyalty_action.adjust_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_unavailable_reply(response.status_code), None)

    reply = f"{target_label} now has {balance} {currency_name}"
    return (reply, {"balance": balance, "target": target_label})


async def _send_reply(
    text: str,
    *,
    community_id: int,
    platform: str,
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply-in-place: resolve the channel, then dispatch via Discord/Twitch.

    Same payload-first/config-fallback channel precedence and Discord/
    Twitch dispatch as `social_music_action.py::_send_reply`.
    """
    payload_channel_id = payload.get("channel_id")
    payload_channel_name = payload.get("channel_name")

    if platform == "twitch":
        channel = payload_channel_name if isinstance(payload_channel_name, str) else None
        if not channel:
            channel = config.get("channel")
    else:
        channel = payload_channel_id if isinstance(payload_channel_id, str) else None
        if not channel:
            channel = config.get("channel_id")

    channel = channel if isinstance(channel, str) and channel else None
    if not channel:
        raise NonRetryableTransportError(
            "community loyalty bundle could not resolve a channel from either "
            "envelope.event.payload['channel_id'/'channel_name'] (reply-in-place) or "
            "config['channel'/'channel_id'] (fallback)"
        )

    logger.debug(
        "community_loyalty_action.reply_channel_resolved community_id=%s platform=%s channel=%s",
        community_id,
        platform,
        channel,
    )

    if platform == "twitch":
        transport = RelayOutboundIrcTransport(
            provider="twitch", redis_client=_get_redis_client(config)
        )
        return await transport.send({"channel": channel}, {"text": text})

    # Discord via guarded_request -- imported lazily, same as social_music_action.py.
    from waddle_transports.signing import SecretResolutionError, resolve_secret
    from waddle_transports.url_guard import SSRFError, guarded_request

    token_ref = config.get("bot_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError(
            "community loyalty bundle config missing required 'bot_token_ref'"
        )

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"discord token resolution failed: {exc}") from exc

    api_base = config.get("api_base", "https://discord.com/api/v10")
    url = f"{api_base}/channels/{channel}/messages"
    # `Bot` auth scheme, never `Bearer` -- matches discord_send_action.py /
    # social_music_action.py's own header for this exact call shape.
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    body = {"content": text}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"discord API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"discord API request failed: {exc}") from exc

    if response.status_code == 429:
        raise RetryableTransportError("discord API rate limited", http_status=429)
    if response.status_code in (401, 403):
        logger.warning(
            "community_loyalty_action.discord_send_rejected community_id=%s bot_token_ref=%s "
            "channel=%s status=%s",
            community_id,
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
        raise RetryableTransportError(
            f"discord API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"loyalty reply sent, channel={channel}",
        http_status=response.status_code,
    )


async def loyalty(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Dispatch `!points`/`!top`/`!shop`/`!redeem` action work against hub-api, then reply.

    Dispatches on `event.payload["subcommand"]` -- the process stage's
    exact outgoing shape (`community_loyalty_process`'s own docstring).
    Raises `NonRetryableTransportError` for a config/payload error or an
    unresolvable reply channel; propagates `Retryable`/
    `NonRetryableTransportError` from the outbound chat send unchanged --
    every hub-api call itself never raises (module docstring).
    """
    payload = envelope.event.payload

    if not envelope.community:
        raise NonRetryableTransportError(
            "community loyalty bundle: envelope.community is None "
            "(tenant-wide activation unsupported)"
        )
    try:
        community_id = int(envelope.community)
    except (TypeError, ValueError) as exc:
        raise NonRetryableTransportError(
            f"community loyalty bundle: community identifier {envelope.community!r} "
            "is not a valid integer"
        ) from exc

    platform = envelope.event.platform.lower() if envelope.event.platform else "discord"
    subcommand = payload.get("subcommand")
    raw_author_id = payload.get("author_id")
    caller_platform_user_id = str(raw_author_id) if raw_author_id is not None else None

    if subcommand == _BALANCE_SUBCOMMAND:
        raw_target = payload.get("target")
        target_label = str(raw_target) if isinstance(raw_target, str) and raw_target else None
        platform_user_id = target_label or caller_platform_user_id
        if not platform_user_id:
            raise NonRetryableTransportError(
                "loyalty action 'balance' requires a resolvable platform_user_id "
                "(neither 'target' nor 'author_id' is present)"
            )
        text, data = await _get_balance(
            http_client,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            target_label=target_label,
        )
    elif subcommand == _TOP_SUBCOMMAND:
        text, data = await _get_leaderboard(http_client, community_id=community_id)
    elif subcommand == _SHOP_SUBCOMMAND:
        text, data = await _get_shop(http_client, community_id=community_id)
    elif subcommand == _REDEEM_SUBCOMMAND:
        raw_sku = payload.get("sku")
        if not isinstance(raw_sku, str) or not raw_sku:
            raise NonRetryableTransportError("loyalty action 'redeem' requires non-empty 'sku'")
        if not caller_platform_user_id:
            raise NonRetryableTransportError(
                "loyalty action 'redeem' requires a resolvable platform_user_id (no 'author_id')"
            )
        text, data = await _redeem(
            http_client,
            community_id=community_id,
            platform=platform,
            platform_user_id=caller_platform_user_id,
            sku=raw_sku,
        )
    elif subcommand == _ADJUST_SUBCOMMAND:
        raw_target = payload.get("target")
        raw_delta = payload.get("delta")
        if not isinstance(raw_target, str) or not raw_target:
            raise NonRetryableTransportError("loyalty action 'adjust' requires non-empty 'target'")
        if not isinstance(raw_delta, int) or isinstance(raw_delta, bool):
            raise NonRetryableTransportError("loyalty action 'adjust' requires integer 'delta'")
        text, data = await _adjust(
            http_client,
            community_id=community_id,
            platform=platform,
            platform_user_id=raw_target,
            delta=raw_delta,
            actor_platform_user_id=caller_platform_user_id,
            target_label=raw_target,
        )
    else:
        raise NonRetryableTransportError(
            f"community loyalty bundle received unknown subcommand {subcommand!r}"
        )

    result = await _send_reply(
        text,
        community_id=community_id,
        platform=platform,
        payload=payload,
        config=config,
        http_client=http_client,
    )

    logger.info(
        "community_loyalty_action.%s community_id=%s platform=%s reply_length=%s data=%s",
        subcommand,
        community_id,
        platform,
        len(text),
        data,
    )

    return result
