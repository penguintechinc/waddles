"""Twitch shoutout action bundle -- `!so`/`!vso` text+video Twitch shoutouts.

Ports the *live* Node.js implementation (`action/interactive/
shoutout_interaction_module/services/{twitch_service.py, video_service.py,
shoutout_service.py, video_shoutout_service.py}` -- app-token Helix
lookups, `{display_name}`/`{login}`/`{game_name}`/`{viewer_count}`
template substitution, a per-`(community, platform, target)` cooldown,
history logging) into the App Bundle SDK's action-stage script contract:
`async def shoutout(envelope, config, *, http_client) -> TransportResult`
(`runner.py`). Entry point name matches the `subcommand` value
(`"shoutout"`) the process stage emits, per gh #316's payload contract:
`{"subcommand": "shoutout", "kind": "text"|"video", "target": <login>}`.

Twitch Helix reads go through `services/twitch_helix.TwitchHelixClient`
(app-access-token auth, never a user OAuth token -- see that module's own
docstring). DB access uses `flask_core.get_bundle_dal()`
(docs/APP_BUNDLE_AUTHORING.md, 'Accessing the database / shared state'),
same pattern `bundles/community_announcements_action.py` already
establishes -- no hub-api internal route exists yet for
`shoutout_config`/`shoutout_history` (only the JWT/admin-scoped
`hub_api/services/bot_shoutout.py`, wired to `/api/v1/admin/:communityId/
shoutout/*` for the admin UI, not a service-key-gated internal route this
poller-driven bundle could call), so this bundle binds its own minimal
`shoutout_config`/`shoutout_history` table stubs directly
(`_ensure_shoutout_tables`, idempotent, `migrate=False` -- schema owned by
`config/postgres/migrations/046_add_remaining_admin_tables.sql`), following
the "bind only the columns this bundle actually touches" convention rather
than the admin service's full column set.

Graceful degradation (task requirement, matching `social_music_action.py`'s
own documented contract): a cooldown hit or a Helix/API failure NEVER
raises out of the lookup/render step -- it becomes a friendly, specific
chat reply (`shoutout failed: <cause>` / `!so <login> is on cooldown for
<N> more minutes`) instead. Only the actual outbound chat SEND
(`_send_reply`, Discord/Twitch) may raise `Retryable`/
`NonRetryableTransportError`, and a config/payload validation error
(missing community, bad `kind`, empty `target`) raises immediately --
both exactly matching every sibling action bundle's own contract. The
video-overlay media push (`kind == "video"`) and the history-log write are
both best-effort past that point: neither failure undoes or fails the
chat reply already sent, matching `community_announcements_action.py`'s
own "log but don't fail" precedent for its audit writes.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx
from flask_core import StageEnvelope, get_bundle_dal
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.transports.irc_relay import RelayOutboundIrcTransport
from waddle_transports.url_guard import SSRFError, guarded_request

from services.twitch_helix import TwitchHelixClient, TwitchHelixError

logger = logging.getLogger(__name__)

#: `!so`/`!vso`'s default text template -- `{display_name}`/`{login}`/
#: `{game_name}` are always supplied (see `shoutout()`), so `.format()`
#: against this string can never raise. A live stream appends the
#: parenthetical viewer-count suffix below rather than baking a second
#: "live" template variant -- one template, one optional suffix.
_DEFAULT_TEMPLATE = (
    "Go check out {display_name} at https://twitch.tv/{login} — they were last playing {game_name}!"
)
_LIVE_SUFFIX_TEMPLATE = " (LIVE now with {viewer_count} viewers)"

_DEFAULT_COOLDOWN_MINUTES = 60
_DEFAULT_VIDEO_DURATION_S = 30
_UNKNOWN_GAME = "something"
_DISCORD_API_BASE = "https://discord.com/api/v10"

#: Lazily-built, process-wide Valkey client for the Twitch IRC relay --
#: same pattern as `twitch_send_action.py`/`social_music_action.py`.
_redis_client: Any | None = None

#: Lazily-built, process-wide Helix client -- preserves its own app-token
#: cache across dispatched envelopes instead of re-minting a fresh OAuth
#: token on every single `!so` (see `TwitchHelixClient`'s own docstring).
_helix_client: TwitchHelixClient | None = None


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


def _get_helix_client(http_client: httpx.AsyncClient) -> TwitchHelixClient:
    """Build (once) or return the process-wide `TwitchHelixClient`."""
    global _helix_client
    if _helix_client is None:
        _helix_client = TwitchHelixClient(http_client)
    return _helix_client


def _ensure_shoutout_tables(dal: Any) -> None:
    """Idempotently bind `shoutout_config`/`shoutout_history` -- only the columns this bundle uses.

    Follows this bundle's own "minimal stub, no DDL" convention --
    `migrate=False` throughout, schema owned by migration 046. Must run on a
    `dal` that already has `communities` defined (svc-action's own `app.py`
    startup discovers it via `await async_dal.reflect()` before
    `set_bundle_dal()`).
    """
    if "shoutout_config" not in dal.tables:
        dal.define_table(
            "shoutout_config",
            dal.Field("community_id", "reference communities", notnull=True),
            dal.Field("cooldown_minutes", "integer", default=_DEFAULT_COOLDOWN_MINUTES),
            migrate=False,
        )
    if "shoutout_history" not in dal.tables:
        dal.define_table(
            "shoutout_history",
            dal.Field("community_id", "reference communities", notnull=True),
            dal.Field("platform", "string", notnull=True),
            dal.Field("target_username", "string", notnull=True),
            dal.Field("shoutout_type", "string", default="text"),
            dal.Field("triggered_by_username", "string"),
            dal.Field("trigger_type", "string", default="manual"),
            dal.Field("created_at", "datetime", default=datetime.utcnow),
            migrate=False,
        )


async def _get_shoutout_config_row(dal: Any, community_id: int) -> Any | None:
    """The community's `shoutout_config` row, or `None` if it has never been provisioned.

    `select_async` expects a pydal `Set` (`dal.dal(query)`), not a bare
    `Query` -- a bare `Query` object has no `.select()`/`.db` of its own in
    this pydal version, matching `runner.py::_resolve_tenant_id`'s own
    `self._dal.dal(self._dal.dal.tenants.slug == tenant_slug)` call shape.
    """
    query = dal.dal.shoutout_config.community_id == community_id
    rows = await dal.select_async(dal.dal(query), limitby=(0, 1))
    return rows[0] if rows else None


async def _last_shoutout_at(
    dal: Any, *, community_id: int, platform: str, target_login: str
) -> datetime | None:
    """Most recent `shoutout_history.created_at` for this `(community, platform, target)` triple."""
    query = (
        (dal.dal.shoutout_history.community_id == community_id)
        & (dal.dal.shoutout_history.platform == platform)
        & (dal.dal.shoutout_history.target_username == target_login)
    )
    rows = await dal.select_async(
        dal.dal(query), orderby=~dal.dal.shoutout_history.created_at, limitby=(0, 1)
    )
    if not rows:
        return None
    created_at = rows[0].created_at
    return created_at if isinstance(created_at, datetime) else None


async def _record_history(
    dal: Any,
    *,
    community_id: int,
    platform: str,
    target_login: str,
    kind: str,
    triggered_by: str | None,
) -> None:
    """Insert one `shoutout_history` row. Raises on a DB write failure -- caller decides."""
    await dal.insert_async(
        dal.shoutout_history,
        community_id=community_id,
        platform=platform,
        target_username=target_login,
        shoutout_type=kind,
        triggered_by_username=triggered_by,
        trigger_type="manual",
    )


def _render_template(template: str, **variables: object) -> str:
    """Render `template`; falls back to the built-in default on a missing `{placeholder}` key.

    A bad community override (a typo'd placeholder) must never break the
    reply entirely -- the default template's own placeholders are always
    among `variables`, so the fallback render can never itself raise.
    """
    try:
        return template.format(**variables)
    except (KeyError, IndexError):
        logger.warning("twitch_shoutout_action.template_override_invalid template=%r", template)
        return _DEFAULT_TEMPLATE.format(**variables)


async def _send_reply(
    text: str,
    *,
    community_id: int,
    platform: str,
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply-in-place: resolve the channel, then dispatch via Twitch IRC relay or Discord REST.

    Same payload-first/config-fallback channel precedence and per-platform
    dispatch as `social_music_action._send_reply` -- each cross-app-routed
    feature action bundle owns its own outbound send since routing goes to
    the FEATURE's own `:action` key, never the bot's.
    """
    payload_channel_id = payload.get("channel_id")
    payload_channel_name = payload.get("channel_name")

    if platform == "twitch":
        channel = payload_channel_name if isinstance(payload_channel_name, str) else None
        if not channel:
            config_channel = config.get("channel")
            channel = config_channel if isinstance(config_channel, str) else None
    else:
        channel = payload_channel_id if isinstance(payload_channel_id, str) else None
        if not channel:
            config_channel_id = config.get("channel_id")
            channel = config_channel_id if isinstance(config_channel_id, str) else None

    channel = channel if isinstance(channel, str) and channel else None
    if not channel:
        raise NonRetryableTransportError(
            "twitch shoutout bundle could not resolve a channel from either "
            "envelope.event.payload['channel_id'/'channel_name'] (reply-in-place) or "
            "config['channel'/'channel_id'] (fallback)"
        )

    logger.debug(
        "twitch_shoutout_action.reply_channel_resolved community_id=%s platform=%s channel=%s",
        community_id,
        platform,
        channel,
    )

    if platform == "twitch":
        transport = RelayOutboundIrcTransport(
            provider="twitch", redis_client=_get_redis_client(config)
        )
        return await transport.send({"channel": channel}, {"text": text})

    token_ref = config.get("bot_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError(
            "twitch shoutout bundle config missing required 'bot_token_ref'"
        )

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"discord token resolution failed: {exc}") from exc

    api_base = config.get("api_base", _DISCORD_API_BASE)
    url = f"{api_base}/channels/{channel}/messages"
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
        detail=f"shoutout reply sent, channel={channel}",
        http_status=response.status_code,
    )


async def _push_video_overlay(
    http_client: httpx.AsyncClient,
    *,
    community_id: int,
    title: str,
    body_text: str,
    image_url: str | None,
    video_url: str | None,
    duration_s: int,
) -> None:
    """Best-effort `POST /overlay/<community>/media/push` on svc-presentation; never raises.

    A push failure (unreachable svc-presentation, SSRF-guard rejection, a
    non-2xx response) only drops the video overlay enhancement -- the chat
    reply (already sent by the caller) is the user-visible part of a
    shoutout and must never be undone by this failing, matching
    `community_announcements_action.py`'s own "log but don't fail"
    precedent for its own best-effort audit writes.
    """
    presentation_base = os.getenv("PRESENTATION_URL", "http://waddlebot-svc-presentation:8207")
    url = f"{presentation_base}/overlay/{community_id}/media/push"
    push_body: dict[str, object] = {
        "title": title,
        "body": body_text,
        "image_url": image_url,
        "video_url": video_url,
        "duration_s": duration_s,
    }
    headers: dict[str, str] = {}
    push_token = os.environ.get("PRESENTATION_PUSH_TOKEN", "")
    if push_token:
        headers["Authorization"] = f"Bearer {push_token}"

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=push_body)
    except SSRFError as exc:
        logger.warning(
            "twitch_shoutout_action.overlay_push_ssrf_rejected community_id=%s error=%s",
            community_id,
            exc,
        )
        return
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        logger.warning(
            "twitch_shoutout_action.overlay_push_failed community_id=%s error=%s",
            community_id,
            exc,
        )
        return

    if response.status_code >= 400:
        logger.warning(
            "twitch_shoutout_action.overlay_push_rejected community_id=%s status=%s",
            community_id,
            response.status_code,
        )
    else:
        logger.debug(
            "twitch_shoutout_action.overlay_push_sent community_id=%s status=%s",
            community_id,
            response.status_code,
        )


async def shoutout(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """`!so`/`!vso` entry point -- cooldown check, Helix lookups, template render, reply, record.

    Expects `envelope.event.payload` shaped `{"subcommand": "shoutout",
    "kind": "text"|"video", "target": <twitch login>}` (the process stage's
    own emitted contract, gh #316). Raises `NonRetryableTransportError` for
    a config/payload error (no community, bad `kind`, empty `target`) or
    an unresolvable reply channel; propagates `Retryable`/
    `NonRetryableTransportError` from the outbound chat send unchanged.
    Every other failure (cooldown hit, Helix/API error) becomes a friendly
    chat reply instead of raising -- see module docstring.
    """
    payload = envelope.event.payload

    if not envelope.community:
        raise NonRetryableTransportError(
            "twitch shoutout bundle: envelope.community is None (tenant-wide activation "
            "unsupported)"
        )
    try:
        community_id = int(envelope.community)
    except (TypeError, ValueError) as exc:
        raise NonRetryableTransportError(
            f"twitch shoutout bundle: community identifier {envelope.community!r} "
            "is not a valid integer"
        ) from exc

    subcommand = payload.get("subcommand")
    if subcommand != "shoutout":
        raise NonRetryableTransportError(
            f"twitch shoutout bundle received unexpected subcommand {subcommand!r}, "
            "expected 'shoutout'"
        )

    kind = payload.get("kind")
    if kind not in ("text", "video"):
        raise NonRetryableTransportError(
            f"twitch shoutout bundle requires payload['kind'] to be 'text' or 'video', got {kind!r}"
        )

    raw_target = payload.get("target")
    if not isinstance(raw_target, str) or not raw_target.strip():
        raise NonRetryableTransportError("twitch shoutout bundle requires a non-empty 'target'")
    target_login = raw_target.strip().lower()

    # Reply-in-place platform: the channel the triggering command came
    # from, defaulting to "twitch" (unlike social_music_action's "discord"
    # default) -- `!so`/`!vso` is this connector's own Twitch chat command
    # first and foremost, even though cross-app routing (gh #298) can in
    # principle deliver it from another platform's ingest.
    platform = envelope.event.platform.lower() if envelope.event.platform else "twitch"

    dal = get_bundle_dal()
    _ensure_shoutout_tables(dal)

    config_row = await _get_shoutout_config_row(dal, community_id)
    cooldown_minutes = _DEFAULT_COOLDOWN_MINUTES
    if config_row is not None:
        row_cooldown = getattr(config_row, "cooldown_minutes", None)
        if isinstance(row_cooldown, int) and row_cooldown > 0:
            cooldown_minutes = row_cooldown

    last_at = await _last_shoutout_at(
        dal, community_id=community_id, platform=platform, target_login=target_login
    )
    if last_at is not None:
        elapsed_seconds = (datetime.utcnow() - last_at).total_seconds()
        remaining_seconds = (cooldown_minutes * 60) - elapsed_seconds
        if remaining_seconds > 0:
            remaining_minutes = max(1, math.ceil(remaining_seconds / 60))
            logger.info(
                "twitch_shoutout_action.cooldown_blocked community_id=%s platform=%s target=%s "
                "remaining_minutes=%s",
                community_id,
                platform,
                target_login,
                remaining_minutes,
            )
            return await _send_reply(
                f"!so {target_login} is on cooldown for {remaining_minutes} more minutes",
                community_id=community_id,
                platform=platform,
                payload=payload,
                config=config,
                http_client=http_client,
            )

    helix = _get_helix_client(http_client)
    try:
        user = await helix.get_user(target_login)
        broadcaster_id = str(user["id"])
        stream = await helix.get_stream(broadcaster_id)

        if stream is not None:
            is_live = True
            game_name = str(stream.get("game_name") or _UNKNOWN_GAME)
            viewer_count = int(stream.get("viewer_count") or 0)
        else:
            is_live = False
            channel = await helix.get_channel(broadcaster_id)
            game_name = str((channel or {}).get("game_name") or _UNKNOWN_GAME)
            viewer_count = 0

        clip = await helix.get_top_clip(broadcaster_id) if kind == "video" else None
    except TwitchHelixError as exc:
        logger.warning(
            "twitch_shoutout_action.helix_failed community_id=%s target=%s error=%s",
            community_id,
            target_login,
            exc,
        )
        return await _send_reply(
            f"shoutout failed: {exc}",
            community_id=community_id,
            platform=platform,
            payload=payload,
            config=config,
            http_client=http_client,
        )

    display_name = str(user.get("display_name") or user.get("login") or target_login)
    login = str(user.get("login") or target_login)

    template_override = getattr(config_row, "template", None) if config_row is not None else None
    template = (
        template_override
        if isinstance(template_override, str) and template_override
        else _DEFAULT_TEMPLATE
    )
    text = _render_template(template, display_name=display_name, login=login, game_name=game_name)
    if is_live:
        text += _LIVE_SUFFIX_TEMPLATE.format(viewer_count=viewer_count)

    logger.debug(
        "twitch_shoutout_action.template_rendered community_id=%s target=%s is_live=%s "
        "custom_template=%s",
        community_id,
        login,
        is_live,
        template_override is not None,
    )

    result = await _send_reply(
        text,
        community_id=community_id,
        platform=platform,
        payload=payload,
        config=config,
        http_client=http_client,
    )

    if kind == "video":
        if clip is not None:
            duration_s = config.get("video_duration_s", _DEFAULT_VIDEO_DURATION_S)
            if not isinstance(duration_s, int) or duration_s <= 0:
                duration_s = _DEFAULT_VIDEO_DURATION_S
            await _push_video_overlay(
                http_client,
                community_id=community_id,
                title=f"Shoutout: {display_name}",
                body_text=text,
                image_url=clip.get("thumbnail_url"),
                video_url=clip.get("embed_url") or clip.get("url"),
                duration_s=duration_s,
            )
        else:
            logger.debug(
                "twitch_shoutout_action.no_clip_for_video community_id=%s target=%s",
                community_id,
                login,
            )

    try:
        await _record_history(
            dal,
            community_id=community_id,
            platform=platform,
            target_login=login,
            kind=kind,
            triggered_by=envelope.event.actor,
        )
    except Exception as exc:  # noqa: BLE001 -- history write failure must never mask the reply sent
        logger.warning(
            "twitch_shoutout_action.history_write_failed community_id=%s target=%s error=%s",
            community_id,
            login,
            exc,
        )

    logger.info(
        "twitch_shoutout_action.sent community_id=%s platform=%s target=%s kind=%s is_live=%s",
        community_id,
        platform,
        login,
        kind,
        is_live,
    )
    return result
