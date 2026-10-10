"""Social music action handler -- enqueues Music Station song requests, `!sr status`, and replies.

Action-stage handler for `!sr`/`!songrequest` (process stage:
`builtin_handlers.social_music_process`, cross-app routed here per gh #298's
`PROCESS_TARGET_APP_ID_KEY` mechanism -- see that handler's docstring).
Calls hub-api's service-key-gated internal Music Station enqueue endpoint
(`POST /api/v1/internal/music/queue/requests`,
`hub_api/blueprints/v1/community_music_queue.py`) -- never the
JWT/admin-scoped `POST /api/v1/admin/<community_id>/music-station/queue/
requests` endpoint, since the requester is a viewer typing a chat
command, not an authenticated hub-api user. Auth mirrors
`core/svc_process/services/reputation_gate_client.py`'s own
`X-Service-Key` pattern against `reputation_module`'s internal endpoint.

`enqueue_song_request()` is also `!sr status`'s entry point -- the SAME
`PROCESS_TARGET_APP_ID_KEY` routing sends BOTH subcommands to this app's
`:action` key, and the entry-point name itself is pinned by `alembic/
versions/0009_music_catalog.py`'s `app_catalog` seed row, so it can't be
renamed/split into two bindings without a migration (out of scope here).
A `music_status_check` payload flag (set by `social_music_process.py`
when the flag is enabled) distinguishes the two at the top of the
function; `_check_status()` calls hub-api's `GET /api/v1/internal/music/
status` and maps the result to one of `!sr status`'s four exact replies.

A third payload shape -- `subcommand="set"`, `key="youtube_allowed_labels"`,
`value: list[str]` (`social_music_process._handle_set_subcommand`'s
successful-parse output) -- routes `!sr set youtube-labels ...` here too,
same `PROCESS_TARGET_APP_ID_KEY` mechanism. `_set_policy()` calls hub-api's
service-key-gated `PUT /api/v1/internal/music/policy` (svc-process can't
mint an admin JWT to call the JWT-scoped policy endpoint directly, same
reasoning as the enqueue path above) and replies `youtube labels set:
<sorted labels>` / `youtube labels cleared -- all videos allowed`. An
unrecognized `key` (only `youtube_allowed_labels` is implemented so far)
replies `song requests: unknown setting '<key>'` without a hub-api round
trip.

A fourth payload shape -- `subcommand="pause"|"resume"` (no other fields;
`social_music_process._handle_pause_resume_subcommand`'s successful-parse
output) -- routes `!sr pause`/`!sr resume` here too, same
`PROCESS_TARGET_APP_ID_KEY` mechanism. `_set_playback()` calls hub-api's
service-key-gated `POST /api/v1/internal/music/playback` and maps the
response's `reason` field to one of five exact chat replies
(`_PLAYBACK_REPLY_BY_REASON`); an unrecognized `reason` falls back to a
generic `song requests: playback <reason>` reply rather than silence. A
4xx response relays hub-api's `error.message` verbatim (same convention
as `_set_policy()`); a 5xx response, a malformed 2xx body, or an
unreachable hub-api (including a timeout) fall back to a generic
`playback unavailable` reply instead.

Reply-in-place: same channel-resolution (payload first, config fallback)
and Discord/Twitch dispatch as `builtin_handlers.social_quote_action`/
`builtin_handlers.discord_send_action`/`builtin_handlers.twitch_send_action` -- each
cross-app-routed feature action handler owns its own outbound send, since
routing goes to the FEATURE's `:action` key, never the bot's own. Shared
by both subcommands via `_send_reply()`.

Graceful degradation (task requirement): an unreachable hub-api, a
provider failure, or no track match NEVER raises out of the enqueue/
status-check step -- `_enqueue()`/`_check_status()` convert every one of
those into a friendly chat reply instead, so the pipeline always has
something to send. Only the actual outbound chat SEND (Discord/Twitch)
may raise Retryable/NonRetryableTransportError, matching every sibling
action handler's own contract.
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

_ENQUEUE_PATH = "/api/v1/internal/music/queue/requests"
_STATUS_PATH = "/api/v1/internal/music/status"
_ENQUEUE_TIMEOUT_SECONDS = 5.0
_STATUS_TIMEOUT_SECONDS = 5.0

_UNAVAILABLE_REPLY = "music requests aren't available right now \U0001f427"
_NOT_FOUND_REPLY = "couldn't find that track \U0001f3b5"

#: Payload flag `social_music_process.py` sets (same string literal convention
#: as `music_query` -- no shared import between the two handler processes) to
#: route an `!sr status` invocation here instead of the enqueue path.
_STATUS_CHECK_KEY = "music_status_check"

#: `!sr status`'s four exact allowed replies (task requirement -- every
#: error path must land on one of these, never silence). `enabled`/`error -
#: <cause>` come from hub-api's own `state`/`cause`; `offline` is this
#: handler's own interpretation of an unreachable/non-2xx/malformed hub-api
#: response -- see `_check_status()`.
_STATUS_ENABLED_REPLY = "song requests: enabled"
_STATUS_OFFLINE_REPLY = "song requests: offline"

_POLICY_PATH = "/api/v1/internal/music/policy"
_POLICY_TIMEOUT_SECONDS = 5.0

#: `event.payload["subcommand"]` value `social_music_process
#: ._handle_set_subcommand` stamps on a successful `!sr set youtube-labels
#: ...` parse -- routes here instead of `_enqueue()`/`_check_status()`.
#: Matches that module's own `"set"` string literal (no shared import
#: between the two handler processes).
_SET_SUBCOMMAND = "set"

#: The only `!sr set <key> ...` key implemented on the hub-api side so far
#: -- matches `social_music_process._YOUTUBE_LABELS_PAYLOAD_KEY` and
#: hub-api's own `PUT .../music-station/policy` field name, so it doubles
#: as both `event.payload["key"]` AND the internal PUT body's field name
#: without a translation table.
_YOUTUBE_LABELS_KEY = "youtube_allowed_labels"

#: `!sr set youtube-labels`'s two success replies (task requirement --
#: sorted labels, or this exact reply when the allowlist is cleared).
_LABELS_CLEARED_REPLY = "youtube labels cleared — all videos allowed"

#: `_set_policy()`'s reply for an unreachable/network-failed hub-api call
#: (a caught `httpx.HTTPError`, including timeouts) -- distinct from
#: `_policy_unavailable_reply()`'s 5xx/malformed-response variant below.
_POLICY_UNREACHABLE_REPLY = "song requests: settings unavailable (hub-api unreachable)"

_PLAYBACK_PATH = "/api/v1/internal/music/playback"
_PLAYBACK_TIMEOUT_SECONDS = 5.0

#: `event.payload["subcommand"]` values `social_music_process
#: ._handle_pause_resume_subcommand` stamps on a successful `!sr pause`/
#: `!sr resume` parse -- routes here instead of `_enqueue()`/
#: `_check_status()`/`_set_policy()`. Matches that module's own string
#: literals (no shared import between the two handler processes) and
#: doubles as the internal POST body's `"action"` field value.
_PAUSE_SUBCOMMAND = "pause"
_RESUME_SUBCOMMAND = "resume"

#: hub-api's `reason` -> this handler's five exact `!sr pause`/`!sr resume`
#: chat replies (task requirement). A `reason` not in this map (future
#: hub-api addition) falls back to a generic `song requests: playback
#: <reason>` reply rather than silence -- see `_set_playback()`.
_PLAYBACK_REPLY_BY_REASON: dict[str, str] = {
    "paused": "song requests paused",
    "resumed": "song requests resumed",
    "already_paused": "song requests are already paused",
    "already_playing": "song requests are already playing",
    "nothing_playing": "nothing is playing right now",
}

#: `_set_playback()`'s reply for an unreachable/network-failed hub-api call
#: (a caught `httpx.HTTPError`, including timeouts) -- distinct from
#: `_playback_unavailable_reply()`'s 5xx/malformed-response variant below.
_PLAYBACK_UNREACHABLE_REPLY = "song requests: playback unavailable (hub-api unreachable)"

#: Lazily-built, process-wide Valkey client for IRC relay (same pattern as
#: twitch_send_action.py / social_quote_action.py).
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


def _format_time_till_played(eta_seconds: int | None, position: int) -> str:
    """Render the third `<time-till-played>` field of the enqueue reply.

    `eta_seconds is None` -- hub-api couldn't compute one, see
    `community_music_queue_service._compute_eta_seconds()`'s own docstring
    for when that happens -- falls back to a bare queue-position count
    (`"<N> ahead"`). `eta_seconds <= 0` means "next up": nothing queued
    ahead of this item and nothing currently playing. Otherwise renders
    `~Xh YYm` (hours, no seconds) or `~Xm YYs` (minutes+seconds) or `~Xs`.
    """
    if eta_seconds is None:
        ahead = max(0, position - 1)
        return f"{ahead} ahead"
    if eta_seconds <= 0:
        return "next up"

    hours, remainder = divmod(eta_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"~{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"~{minutes}m {seconds:02d}s"
    return f"~{seconds}s"


async def _enqueue(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    url_or_query: str,
    platform: str,
    platform_user_id: str | None,
    requested_by_display: str | None,
) -> tuple[str, dict[str, object] | None]:
    """POST to hub-api's internal Music Station enqueue endpoint; never raises.

    Returns a tuple of (chat reply text, structured data dict or None) for every
    outcome. The reply text is sent to chat; the data dict contains parsed
    track/queue info for structured logging on success. Failure paths return
    None for the data dict.
    """
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")
    body = {
        "communityId": community_id,
        "urlOrQuery": url_or_query,
        "platform": platform,
        "platformUserId": platform_user_id,
        "requestedByDisplay": requested_by_display,
    }

    logger.debug(
        "social_music_action.enqueue_request community_id=%s platform=%s", community_id, platform
    )

    try:
        response = await http_client.post(
            f"{hub_api_base}{_ENQUEUE_PATH}",
            json=body,
            headers={"X-Service-Key": service_api_key},
            timeout=_ENQUEUE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "social_music_action.hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_UNAVAILABLE_REPLY, None)

    logger.debug(
        "social_music_action.enqueue_response community_id=%s status=%s",
        community_id,
        response.status_code,
    )

    if response.status_code >= 400:
        message = ""
        try:
            error_body = response.json()
            message = str((error_body.get("error") or {}).get("message", ""))
        except ValueError:
            pass
        logger.warning(
            "social_music_action.enqueue_rejected community_id=%s status=%s message=%s "
            "url=%s body=%s",
            community_id,
            response.status_code,
            message,
            response.request.url
            if response.request is not None
            else f"{hub_api_base}{_ENQUEUE_PATH}",
            response.text[:300],
        )
        if "no track found" in message.lower():
            return (_NOT_FOUND_REPLY, None)
        return (_UNAVAILABLE_REPLY, None)

    try:
        data = response.json()
        item = data["item"]
        track = item["track"]
        title = str(track["title"])
        artist = str(track["artist"])
        position = int(item["position"])
        eta_raw = item.get("etaSeconds")
        eta_is_number = isinstance(eta_raw, (int, float)) and not isinstance(eta_raw, bool)
        eta_seconds = int(eta_raw) if eta_is_number else None
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "social_music_action.malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_UNAVAILABLE_REPLY, None)

    time_till_played = _format_time_till_played(eta_seconds, position)
    logger.debug(
        "social_music_action.enqueue_reply_chosen community_id=%s position=%s eta_seconds=%s",
        community_id,
        position,
        eta_seconds,
    )
    reply = f"added to the queue: {title} - {artist} - {time_till_played}"
    data_dict: dict[str, object] = {
        "title": title,
        "artist": artist,
        "position": position,
        "eta_seconds": eta_seconds,
        "request_id": item.get("id"),
    }
    return (reply, data_dict)


def _provider_status_fragment(name: str, info: object) -> str:
    """Render one provider's `providers.<name>` sub-status as one `!sr status` reply fragment.

    `"enabled"` -> `"<name> ✓"`; `"not_configured"` -> `"<name>: not
    configured"`; anything else (including a malformed/missing `info`) ->
    `"<name>: error - <cause>"`, defaulting `cause` to `"unknown error"`.
    """
    provider_info = info if isinstance(info, dict) else {}
    provider_state = str(provider_info.get("state", ""))
    if provider_state == "enabled":
        return f"{name} ✓"
    if provider_state == "not_configured":
        return f"{name}: not configured"
    cause = provider_info.get("cause")
    cause_text = str(cause) if cause else "unknown error"
    return f"{name}: error - {cause_text}"


def _format_enabled_status_reply(providers: Mapping[str, Any]) -> str:
    """Render `!sr status`'s per-provider `enabled` reply from hub-api's `data.providers`."""
    youtube_fragment = _provider_status_fragment("youtube", providers.get("youtube"))
    spotify_fragment = _provider_status_fragment("spotify", providers.get("spotify"))
    return f"song requests: enabled ({youtube_fragment}, {spotify_fragment})"


async def _check_status(
    http_client: httpx.AsyncClient, *, community_id: int
) -> tuple[str, dict[str, object] | None]:
    """GET hub-api's internal music-status endpoint; maps it to one of `!sr status`'s replies.

    Returns a tuple of (reply text, structured data dict or None). Never
    raises, never returns silence. `offline` covers an unreachable hub-api,
    a non-2xx response, AND a malformed 2xx body -- hub-api's own top-level
    `state` is only ever `"enabled"`/`"error"`, see that endpoint's own
    docstring for why `"offline"` is entirely this function's call.

    When hub-api's response carries a `data.providers` breakdown (Spotify +
    YouTube), the reply is a per-provider composite
    (`_format_enabled_status_reply()` when enabled, `"error - <spotify
    cause>; youtube: <cause>"` when neither provider works). An
    older-format response with no `providers` key (back-compat) falls back
    to the original bare `"enabled"`/`"error - <cause>"` replies.
    """
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")

    logger.debug("social_music_action.status_check_request community_id=%s", community_id)

    try:
        response = await http_client.get(
            f"{hub_api_base}{_STATUS_PATH}",
            params={"community_id": community_id},
            headers={"X-Service-Key": service_api_key},
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "social_music_action.status_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_STATUS_OFFLINE_REPLY, None)

    logger.debug(
        "social_music_action.status_response community_id=%s status=%s",
        community_id,
        response.status_code,
    )

    if response.status_code >= 400:
        logger.warning(
            "social_music_action.status_rejected community_id=%s status=%s body=%s",
            community_id,
            response.status_code,
            response.text[:300],
        )
        return (_STATUS_OFFLINE_REPLY, None)

    try:
        payload = response.json()
        data = payload["data"]
        state = str(data["state"])
        cause = data.get("cause")
        providers_raw = data.get("providers")
        providers = providers_raw if isinstance(providers_raw, dict) else None
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "social_music_action.status_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_STATUS_OFFLINE_REPLY, None)

    logger.debug(
        "social_music_action.status_reply_chosen community_id=%s state=%s has_providers=%s",
        community_id,
        state,
        providers is not None,
    )
    data_dict: dict[str, object] = {"state": state}
    if state == "enabled":
        reply = _format_enabled_status_reply(providers) if providers else _STATUS_ENABLED_REPLY
        return (reply, data_dict)
    if state == "error":
        cause_text = str(cause) if cause else "unknown error"
        data_dict["cause"] = cause_text
        if providers:
            youtube_cause = providers.get("youtube")
            youtube_cause = (
                youtube_cause.get("cause") if isinstance(youtube_cause, dict) else None
            )
            youtube_cause_text = str(youtube_cause) if youtube_cause else "unknown error"
            data_dict["youtube_cause"] = youtube_cause_text
            reply = f"song requests: error - {cause_text}; youtube: {youtube_cause_text}"
            return (reply, data_dict)
        return (f"song requests: error - {cause_text}", data_dict)
    # Unknown/unexpected state from hub-api -- safest reply is offline, never silence.
    return (_STATUS_OFFLINE_REPLY, None)


def _policy_unavailable_reply(status_code: int) -> str:
    """Render the `settings unavailable` reply for a 5xx or malformed hub-api policy response.

    Distinct from `_POLICY_UNREACHABLE_REPLY` (network failure/timeout --
    no status code to report) -- see `_set_policy()`.
    """
    return f"song requests: settings unavailable (hub-api error {status_code})"


async def _set_policy(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    key: str,
    value: list[str],
) -> tuple[str, dict[str, object] | None]:
    """PUT hub-api's internal Music Station policy endpoint; never raises.

    Returns (chat reply text, structured data dict or None), same contract
    as `_enqueue()`/`_check_status()`. Task requirement: a 4xx response
    relays hub-api's `error.message` verbatim (it's user-facing); a 5xx
    response, a malformed 2xx body, or an unreachable hub-api (including a
    timeout) fall back to a generic `settings unavailable` reply instead
    (`_policy_unavailable_reply()` / `_POLICY_UNREACHABLE_REPLY`).
    """
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")
    body = {"community_id": community_id, key: value}

    logger.debug(
        "social_music_action.policy_update_request community_id=%s key=%s count=%d",
        community_id,
        key,
        len(value),
    )

    try:
        response = await http_client.put(
            f"{hub_api_base}{_POLICY_PATH}",
            json=body,
            headers={"X-Service-Key": service_api_key},
            timeout=_POLICY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "social_music_action.policy_hub_api_unreachable community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_POLICY_UNREACHABLE_REPLY, None)

    logger.debug(
        "social_music_action.policy_response community_id=%s status=%s",
        community_id,
        response.status_code,
    )

    if 400 <= response.status_code < 500:
        message = ""
        try:
            error_body = response.json()
            message = str((error_body.get("error") or {}).get("message", ""))
        except ValueError:
            pass
        logger.warning(
            "social_music_action.policy_rejected community_id=%s status=%s message=%s",
            community_id,
            response.status_code,
            message,
        )
        return (message or _policy_unavailable_reply(response.status_code), None)

    if response.status_code >= 500:
        logger.warning(
            "social_music_action.policy_server_error community_id=%s status=%s",
            community_id,
            response.status_code,
        )
        return (_policy_unavailable_reply(response.status_code), None)

    try:
        data = response.json()["data"]
        labels_raw = data[key]
        if not isinstance(labels_raw, list):
            raise TypeError(f"{key!r} is not a list")
        labels = [str(label) for label in labels_raw]
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "social_music_action.policy_malformed_response community_id=%s error=%s",
            community_id,
            exc,
        )
        return (_policy_unavailable_reply(response.status_code), None)

    logger.debug(
        "social_music_action.policy_reply_chosen community_id=%s key=%s count=%s",
        community_id,
        key,
        len(labels),
    )
    reply = f"youtube labels set: {', '.join(sorted(labels))}" if labels else _LABELS_CLEARED_REPLY
    data_dict: dict[str, object] = {"key": key, "count": len(labels)}
    return (reply, data_dict)


def _playback_unavailable_reply(status_code: int) -> str:
    """Render the `playback unavailable` reply for a 5xx or malformed hub-api playback response.

    Distinct from `_PLAYBACK_UNREACHABLE_REPLY` (network failure/timeout --
    no status code to report) -- see `_set_playback()`.
    """
    return f"song requests: playback unavailable (hub-api error {status_code})"


async def _set_playback(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    action: str,
) -> tuple[str, dict[str, object] | None]:
    """POST hub-api's internal Music Station playback endpoint; never raises.

    Returns (chat reply text, structured data dict or None), same contract
    as `_enqueue()`/`_check_status()`/`_set_policy()`. Task requirement: a
    4xx response relays hub-api's `error.message` verbatim (it's user-
    facing); a 5xx response, a malformed 2xx body, or an unreachable
    hub-api (including a timeout) fall back to a generic `playback
    unavailable` reply instead (`_playback_unavailable_reply()` /
    `_PLAYBACK_UNREACHABLE_REPLY`). A successful response's `reason` maps
    to one of `_PLAYBACK_REPLY_BY_REASON`'s five exact replies; an
    unrecognized `reason` falls back to a generic `song requests: playback
    <reason>` reply rather than silence.
    """
    hub_api_base = os.getenv("HUB_API_URL", "http://hub-api:8204")
    service_api_key = os.getenv("SERVICE_API_KEY", "")
    body = {"community_id": community_id, "action": action}

    logger.debug(
        "social_music_action.playback_request community_id=%s action=%s", community_id, action
    )

    try:
        response = await http_client.post(
            f"{hub_api_base}{_PLAYBACK_PATH}",
            json=body,
            headers={"X-Service-Key": service_api_key},
            timeout=_PLAYBACK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "social_music_action.playback_hub_api_unreachable community_id=%s action=%s error=%s",
            community_id,
            action,
            exc,
        )
        return (_PLAYBACK_UNREACHABLE_REPLY, None)

    logger.debug(
        "social_music_action.playback_response community_id=%s action=%s status=%s",
        community_id,
        action,
        response.status_code,
    )

    if 400 <= response.status_code < 500:
        message = ""
        try:
            error_body = response.json()
            message = str((error_body.get("error") or {}).get("message", ""))
        except ValueError:
            pass
        logger.warning(
            "social_music_action.playback_rejected community_id=%s action=%s status=%s "
            "message=%s",
            community_id,
            action,
            response.status_code,
            message,
        )
        return (message or _playback_unavailable_reply(response.status_code), None)

    if response.status_code >= 500:
        logger.warning(
            "social_music_action.playback_server_error community_id=%s action=%s status=%s",
            community_id,
            action,
            response.status_code,
        )
        return (_playback_unavailable_reply(response.status_code), None)

    try:
        data = response.json()["data"]
        reason = str(data["reason"])
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "social_music_action.playback_malformed_response community_id=%s action=%s error=%s",
            community_id,
            action,
            exc,
        )
        return (_playback_unavailable_reply(response.status_code), None)

    reply = _PLAYBACK_REPLY_BY_REASON.get(reason, f"song requests: playback {reason}")
    logger.debug(
        "social_music_action.playback_reply_chosen community_id=%s action=%s reason=%s",
        community_id,
        action,
        reason,
    )
    data_dict: dict[str, object] = {"reason": reason}
    return (reply, data_dict)


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

    Shared by both `enqueue_song_request()`'s subcommand paths (enqueue,
    status check) -- same precedence as `social_quote_action.py`: payload
    channel first, config fallback.
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
            "social music bundle could not resolve a channel from either "
            "envelope.event.payload['channel_id'/'channel_name'] (reply-in-place) or "
            "config['channel'/'channel_id'] (fallback)"
        )

    logger.debug(
        "social_music_action.reply_channel_resolved community_id=%s platform=%s channel=%s",
        community_id,
        platform,
        channel,
    )

    if platform == "twitch":
        transport = RelayOutboundIrcTransport(
            provider="twitch", redis_client=_get_redis_client(config)
        )
        return await transport.send({"channel": channel}, {"text": text})

    # Discord via guarded_request -- imported lazily, same as social_quote_action.py.
    from waddle_transports.signing import SecretResolutionError, resolve_secret
    from waddle_transports.url_guard import SSRFError, guarded_request

    token_ref = config.get("bot_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError(
            "social music bundle config missing required 'bot_token_ref'"
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
    # Matches discord_send_action.py:send_message's own header, the platform's
    # other Discord-reply handler.
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
            "social_music_action.discord_send_rejected community_id=%s bot_token_ref=%s "
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
        detail=f"music request reply sent, channel={channel}",
        http_status=response.status_code,
    )


async def enqueue_song_request(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Dispatch `!sr`/`!songrequest` action work -- enqueue a song, or `!sr status`.

    Entry point name is pinned by `alembic/versions/0009_music_catalog.py`'s
    `app_catalog` seed row (`"entrypoint": "builtin_handlers.social_music_action:
    enqueue_song_request"`) -- kept unchanged (module docstring) even
    though a `music_status_check` payload flag now routes some events to
    `_check_status()` instead of `_enqueue()`, `subcommand="set"` routes
    others to `_set_policy()`, and `subcommand="pause"|"resume"` routes
    others still to `_set_playback()` -- see module docstring.

    Requester identity is read from the SAME already-tokenized fields
    every other handler uses -- `event.payload["author_id"]` (platform-
    native user id, never raw PII) and `event.actor` (display name) --
    never re-derived here.

    Raises `NonRetryableTransportError` for a config/payload error or an
    unresolvable reply channel; propagates `Retryable`/
    `NonRetryableTransportError` from the outbound chat send unchanged --
    see module docstring for why the enqueue/status-check step itself
    never raises.
    """
    payload = envelope.event.payload

    if not envelope.community:
        raise NonRetryableTransportError(
            "social music bundle: envelope.community is None (tenant-wide activation unsupported)"
        )
    try:
        community_id = int(envelope.community)
    except (TypeError, ValueError) as exc:
        raise NonRetryableTransportError(
            f"social music bundle: community identifier {envelope.community!r} "
            "is not a valid integer"
        ) from exc

    platform = envelope.event.platform.lower() if envelope.event.platform else "discord"

    # Resolve channel for logging
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

    subcommand = payload.get("subcommand")

    if payload.get(_STATUS_CHECK_KEY):
        logger.debug("social_music_action.dispatch_status_check community_id=%s", community_id)
        text, data = await _check_status(http_client, community_id=community_id)
    elif subcommand == _SET_SUBCOMMAND:
        key = payload.get("key")
        value = payload.get("value")
        logger.debug("social_music_action.dispatch_set community_id=%s key=%s", community_id, key)
        if key != _YOUTUBE_LABELS_KEY:
            logger.debug(
                "social_music_action.set_unknown_key community_id=%s key=%s", community_id, key
            )
            text, data = (f"song requests: unknown setting '{key}'", None)
        else:
            if not isinstance(value, list):
                raise NonRetryableTransportError("music action 'set' requires list 'value'")
            labels = [str(item) for item in value]
            text, data = await _set_policy(
                http_client, community_id=community_id, key=key, value=labels
            )
    elif subcommand in (_PAUSE_SUBCOMMAND, _RESUME_SUBCOMMAND):
        logger.debug(
            "social_music_action.dispatch_playback community_id=%s action=%s",
            community_id,
            subcommand,
        )
        text, data = await _set_playback(
            http_client, community_id=community_id, action=str(subcommand)
        )
    else:
        query = payload.get("music_query")
        if not isinstance(query, str) or not query.strip():
            raise NonRetryableTransportError("music action requires 'music_query'")
        raw_author_id = payload.get("author_id")
        platform_user_id = str(raw_author_id) if raw_author_id is not None else None
        logger.debug("social_music_action.dispatch_enqueue community_id=%s", community_id)
        text, data = await _enqueue(
            http_client,
            community_id=community_id,
            url_or_query=query.strip(),
            platform=platform,
            platform_user_id=platform_user_id,
            requested_by_display=envelope.event.actor,
        )

    result = await _send_reply(
        text,
        community_id=community_id,
        platform=platform,
        payload=payload,
        config=config,
        http_client=http_client,
    )

    # Log success with structured data
    if data is not None:
        if payload.get(_STATUS_CHECK_KEY):
            logger.info(
                "social_music_action.status_replied community_id=%s platform=%s channel=%s "
                "state=%s reply_length=%s",
                community_id,
                platform,
                channel,
                data.get("state"),
                len(text),
            )
        elif subcommand == _SET_SUBCOMMAND:
            logger.info(
                "social_music_action.policy_updated community_id=%s platform=%s channel=%s "
                "key=%s count=%s reply_length=%s",
                community_id,
                platform,
                channel,
                data.get("key"),
                data.get("count"),
                len(text),
            )
        elif subcommand in (_PAUSE_SUBCOMMAND, _RESUME_SUBCOMMAND):
            logger.info(
                "social_music_action.playback_changed community_id=%s action=%s reason=%s "
                "platform=%s channel=%s reply_length=%s",
                community_id,
                subcommand,
                data.get("reason"),
                platform,
                channel,
                len(text),
            )
        else:
            logger.info(
                "social_music_action.enqueued community_id=%s platform=%s channel=%s "
                "title=%s artist=%s position=%s eta_seconds=%s request_id=%s reply_length=%s",
                community_id,
                platform,
                channel,
                data.get("title"),
                data.get("artist"),
                data.get("position"),
                data.get("eta_seconds"),
                data.get("request_id"),
                len(text),
            )

    return result
