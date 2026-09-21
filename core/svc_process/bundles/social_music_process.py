"""Social music process bundle -- parses `!sr`/`!songrequest` chat commands.

Normalizes a chat song-request command into a structured `PlatformEvent`
for the action stage (`bundles.social_music_action`), which enqueues the
resolved track into the hub-api Music Station queue
(`hub_api/blueprints/v1/community_music_queue.py`) and replies in-place.

Supports two aliases for the same command:
- `!sr <url or search query>`
- `!songrequest <url or search query>`

Plus two subcommands (checked before the free-text query is treated as a
song):
- `!sr status` -- health check, replies `song requests: enabled|disabled|
  offline|error - <cause>`. `disabled` is answered HERE, directly, without
  calling hub-api (this bundle already knows the flag state -- see below);
  `enabled`/`offline`/`error - <cause>` require hub-api's real, cached
  Spotify health probe, so those route to the action stage same as a song
  request (`social_music_action._check_status()`).
- `!sr set <key> <value>` -- settings management. Only `youtube-labels` is
  implemented (see below); every other key replies `song requests: unknown
  setting '<key>' — supported: youtube-labels`, and a bare `!sr set` (no
  key at all) or the `waddles.social.music.youtube_labels` flag being off
  replies `song requests: 'set' is not available yet`.

`!sr set youtube-labels <label,label,...>` -- admin/moderator-only. Sets
the YouTube video-title label allowlist enforced by hub-api's Music
Station policy (`youtube_allowed_labels`,
`hub_api/blueprints/v1/community_music_queue.py`'s
`PUT .../music-station/policy`). This bundle never calls hub-api directly
(svc-process can't mint an admin JWT) -- like a song request or `!sr
status`, a successful parse stamps `PROCESS_TARGET_APP_ID_KEY` and routes
to `social_music_action`, which performs the service-key-authenticated
`PUT /api/v1/internal/music/policy` call and sends the actual chat reply
(`youtube labels set: ...` / `youtube labels cleared — all videos
allowed`). The outgoing event carries `subcommand="set"`,
`key="youtube_allowed_labels"` (matching the hub-api field name so the
action stage can forward `value` without a translation table), and
`value: list[str]` (parsed: split on commas, trimmed, lowercased, empties
dropped, deduped, first-seen order; `none`/`clear`/no labels -> `[]`).
Limits: max 32 labels, 64 chars each, else `youtube-labels: up to 32
labels, 64 chars each`. No value at all (`!sr set youtube-labels`) ->
usage hint, no hub-api round trip. Gated by its own PostHog flag,
`waddles.social.music.youtube_labels` (default ON, independent of
`_FEATURE_FLAG` which must already be on to reach subcommand parsing at
all) -- off falls back to the same `'set' is not available yet` reply as
an unimplemented key. Permission: community admin/moderator only, same
`community_members` role-lookup convention `social_alias_process
._caller_is_moderator_or_admin` uses (match by `(platform,
platform_user_id)` first, else `display_name == event.actor`, fail
closed) -- replicated locally rather than imported, since these bundle
modules don't cross-import each other's internals; deny reply `only
moderators/admins can change song request settings`.

`!sr pause` / `!sr resume` -- moderator/admin-only playback controls for
the community's Music Station. Same permission gate as `!sr set
youtube-labels` (`_caller_is_moderator_or_admin`, replicated locally per
this module's own dependency convention -- see above); a denied caller
gets `_PAUSE_RESUME_PERMISSION_DENIED_REPLY` directly, no hub-api round
trip. An allowed caller's event is routed to the action stage the same
way `set`'s successful parse is (`PROCESS_TARGET_APP_ID_KEY` stamped,
`social_music_action` performs the actual service-key-authenticated call
and sends the reply) -- but stamps only `subcommand="pause"|"resume"`, no
`key`/`value`. Gated by the same `_FEATURE_FLAG` as every other `!sr`
subcommand (already required ON to reach subcommand parsing at all); flag
OFF behaves like an unrecognized command, same as `set`/a song request.

ROUTING (mirrors `bundles.community_forums_process`'s gh #298 mechanism):
a successful parse -- but not a usage-hint/disabled-status/unavailable-set
reply -- stamps `PROCESS_TARGET_APP_ID_KEY` onto the returned event's
payload with `_MUSIC_APP_ID`. `bot_process.py` delegates `!sr`/
`!songrequest` to this bundle's `transform()` in-process and returns
whatever it gets back unmodified, so this key rides all the way to
`core/svc_process/runner.py`, which enqueues the event onto the music
app's `:action` key instead of the originating bot's -- see
`PROCESS_TARGET_APP_ID_KEY`'s docstring in `flask_core.stream_pipeline`
for the full mechanism.

Requester identity: the requesting viewer's platform-native user id
(`event.payload["author_id"]`) and display name (`event.actor`) are
already tokenized, opaque platform identifiers set upstream by ingest --
never raw PII -- and this bundle never strips or overwrites them; they
ride through to the action stage untouched via `**event.payload`, same
as `channel_id`/`channel_name`.

Feature-gated (default OFF) via `flask_core.feature_flags.feature_enabled`
-- `waddles.social.music` -- following `services.moderation_gate`'s own
direct-call pattern (this command family predates any Feature-contract
registry entry for bot/social command bundles; `libs/core_platform_module/
features.py` covers a different, unrelated set of 14 Core/Platform
features and is not extended here). Flag OFF (or a PostHog/license-server
outage, which `feature_enabled` itself degrades to `default=False` for)
means `!sr`/`!songrequest` behaves like an unrecognized command (no
reply) for every subcommand EXCEPT `status`, which must always answer --
see `_STATUS_SUBCOMMAND` handling in `transform()`.

`!sq`/`!songqueue` -- a fully separate, sibling command (own aliases, own
flag, own reply), NOT a subcommand of `!sr`:
- `!sq [anything]` / `!songqueue [anything]` -- replies `song queue:
  <link>`, where `<link>` is `PUBLIC_WEBUI_URL` (stripped of a trailing
  slash) + `/c/{community_id}/music/queue` -- the public queue page. Any
  trailing argument is ignored; the reply is always the same link.
- Handled entirely in svc-process -- no hub-api round trip, no
  `PROCESS_TARGET_APP_ID_KEY` cross-app routing (unlike a song request or
  `!sr status`) -- there is nothing for the action stage to do.
- Gated by its own PostHog flag, `waddles.social.music.queue_page`,
  checked via the same `feature_enabled` helper -- independent of `!sr`'s
  `waddles.social.music` flag. Default **ON** (alpha) unlike `!sr`; flag
  off (or an outage, same degradation as above) means no reply, DEBUG
  logged.
- `PUBLIC_WEBUI_URL` is read via a tiny cached accessor
  (`_public_webui_url()`, `os.getenv` + `functools.lru_cache` -- `config.py`
  is off-limits this round). Unset/blank -> replies `song queue: link not
  configured (PUBLIC_WEBUI_URL)` and logs a WARN once per process (the
  `lru_cache` means the accessor body, and therefore the warning, only
  ever runs on the first call).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from functools import lru_cache

from flask_core import (
    PROCESS_TARGET_APP_ID_KEY,
    BundleContext,
    PlatformEvent,
    get_bundle_context,
    get_bundle_dal,
)
from flask_core.bundle_runtime import raw_sql_rows
from flask_core.feature_flags import feature_enabled

logger = logging.getLogger(__name__)

#: Matches either alias, valid or not -- used to tell "this is a song
#: request command with a missing/blank query" (usage hint) apart from
#: "not a song request at all" (`None`, no reply).
_SR_PREFIX_RE = re.compile(r"^!(sr|songrequest)\b", re.IGNORECASE)

_SR_USAGE = "Usage: !sr <url or search query>  ·  !songrequest <url or search query>"

#: `app_catalog.app_id` this bundle's action stage is registered under
#: (alembic 0009_music_catalog). A successful parse routes to THIS app's
#: `:action` key instead of the originating bot's -- see module docstring.
_MUSIC_APP_ID = "waddles.social.music.default"

#: PostHog flag key, `waddles.<module>.<feature>` convention (see
#: `waddles.community.forums`/`waddles.social.quote`/`waddles.analytics.*`
#: elsewhere in this repo). Default OFF until validated for the demo.
_FEATURE_FLAG = "waddles.social.music"

#: `!sr status`'s subcommand text (case-insensitive, matched against the
#: already-lowercased query).
_STATUS_SUBCOMMAND = "status"
_STATUS_DISABLED_REPLY = "song requests: disabled"

#: Payload flag routing an `!sr status` event to `social_music_action.
#: _check_status()` instead of `_enqueue()` -- same string-literal
#: convention as `music_query` (no shared import between the two bundle
#: processes; see that module's own `_STATUS_CHECK_KEY`).
_STATUS_CHECK_KEY = "music_status_check"

#: `!sr set ...` -- matched so it isn't misinterpreted as a song
#: title/search query. Only `youtube-labels` is implemented (see below);
#: a bare `!sr set`, an unimplemented key, or the youtube-labels flag
#: being off all fall back to this reply.
_SET_SUBCOMMAND = "set"
_SET_UNAVAILABLE_REPLY = "song requests: 'set' is not available yet"

#: PostHog flag key gating the `youtube-labels` `!sr set` key specifically
#: -- independent of `_FEATURE_FLAG` (`!sr`'s parent flag, already
#: required to be on to reach subcommand parsing at all). Default ON.
_YOUTUBE_LABELS_FEATURE_FLAG = "waddles.social.music.youtube_labels"

#: The only `!sr set <key> ...` key implemented so far.
_YOUTUBE_LABELS_KEY_COMMAND = "youtube-labels"

#: `event.payload["key"]` value stamped on a successful youtube-labels
#: set/clear -- matches the hub-api Music Station policy field name
#: (`youtube_allowed_labels`) so `social_music_action` can forward
#: `event.payload["value"]` straight into the internal policy call without
#: a translation table.
_YOUTUBE_LABELS_PAYLOAD_KEY = "youtube_allowed_labels"

_MAX_YOUTUBE_LABELS = 32
_MAX_YOUTUBE_LABEL_LEN = 64

_YOUTUBE_LABELS_USAGE_REPLY = "usage: !sr set youtube-labels <label,label,...> | none"
_YOUTUBE_LABELS_LIMIT_REPLY = "youtube-labels: up to 32 labels, 64 chars each"
_SET_PERMISSION_DENIED_REPLY = "only moderators/admins can change song request settings"

#: `!sr pause`/`!sr resume` -- matched so neither is misinterpreted as a
#: song title/search query. Both route through the same permission gate
#: and action-stage handoff (module docstring); a trailing argument
#: (`!sr pause <anything>`) is ignored, same convention as `status`/`set`.
_PAUSE_SUBCOMMAND = "pause"
_RESUME_SUBCOMMAND = "resume"

#: `!sr pause`/`!sr resume`'s single denial reply -- shared by both
#: subcommands (task requirement), unlike `!sr set`'s own denial reply.
_PAUSE_RESUME_PERMISSION_DENIED_REPLY = "only moderators/admins can pause song requests"

#: `!sr set youtube-labels <value>` values that clear the allowlist
#: (`value` -> `[]`) rather than being parsed as labels.
_YOUTUBE_LABELS_CLEAR_VALUES = frozenset({"none", "clear"})

#: `community_members.role` values authorized to change song request
#: settings -- same vocabulary `social_alias_process._ADMIN_ROLES` uses
#: (original chat-role vocabulary plus the newer web-authz slugs);
#: replicated locally rather than imported (see module docstring).
_ADMIN_ROLES = frozenset({"owner", "admin", "moderator", "community-owner", "community-admin"})

_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND platform = :platform "
    "AND platform_user_id = :platform_user_id LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND display_name = :display_name LIMIT 1"
)

#: Matches `!sq`/`!songqueue`, either alias -- a fully separate sibling
#: command from `!sr`/`!songrequest` (own flag, own reply, no hub-api
#: routing -- see module docstring).
_SQ_PREFIX_RE = re.compile(r"^!(sq|songqueue)\b", re.IGNORECASE)

#: PostHog flag key for `!sq`/`!songqueue`, independent of `_FEATURE_FLAG`
#: (`!sr`'s flag) -- default ON for the alpha demo (see the
#: `feature_enabled` call in `_handle_song_queue`).
_QUEUE_FEATURE_FLAG = "waddles.social.music.queue_page"

_QUEUE_LINK_NOT_CONFIGURED = "song queue: link not configured (PUBLIC_WEBUI_URL)"


def _text_reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a direct chat reply (no cross-app routing), preserving every other payload field.

    Used for the usage hint, the disabled-status reply, and the
    not-yet-available `set` reply -- none of these need hub-api, so they
    never stamp `PROCESS_TARGET_APP_ID_KEY` and stay on the originating
    bot's own reply-in-place pipeline.
    """
    return dataclasses.replace(event, payload={**event.payload, "text": text})


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the flag check; `None`/unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _public_webui_url() -> str | None:
    """Read and cache `PUBLIC_WEBUI_URL`, stripped of any trailing slash.

    `config.py` is off-limits this round (see module docstring), hence a
    direct `os.getenv` here rather than a `Config` attribute. Cached for
    the life of the process (`functools.lru_cache`, same pattern as
    `flask_core.platform_version.get_platform_version`) -- this env var
    cannot change mid-process, and caching also means the "unset" WARN log
    below fires at most once per process, not once per `!sq`/`!songqueue`
    command.

    Returns:
        The configured base URL with any trailing slash stripped, or
        `None` if `PUBLIC_WEBUI_URL` is unset/blank.
    """
    raw = os.getenv("PUBLIC_WEBUI_URL")
    if not raw:
        logger.warning("social_music_process.public_webui_url_not_configured")
        return None
    return raw.rstrip("/")


async def _handle_song_queue(event: PlatformEvent) -> PlatformEvent | None:
    """Handle `!sq`/`!songqueue` -- reply with a link to the public song-queue page.

    Handled entirely in svc-process: no hub-api round trip and no
    `PROCESS_TARGET_APP_ID_KEY` cross-app routing (unlike a song request or
    `!sr status`) -- there is nothing for the action stage to do, the reply
    is a static link built from `PUBLIC_WEBUI_URL` + the resolved
    community. A trailing argument (`!sq <anything>`) is ignored; the
    reply is always the same link.
    """
    ctx = get_bundle_context()
    enabled = await feature_enabled(
        _QUEUE_FEATURE_FLAG,
        tenant=ctx.tenant,
        community=_community_id(ctx.community),
        default=True,
    )
    logger.debug(
        "social_music_process.queue_flag_checked enabled=%s community=%s", enabled, ctx.community
    )
    if not enabled:
        logger.debug("social_music_process.queue_flag_disabled_no_reply")
        return None  # feature disabled -- behaves like an unrecognized command

    base_url = _public_webui_url()
    if base_url is None:
        logger.debug("social_music_process.queue_link_not_configured_reply")
        return _text_reply(event, _QUEUE_LINK_NOT_CONFIGURED)

    link = f"{base_url}/c/{ctx.community}/music/queue"
    logger.debug("social_music_process.queue_link_reply community=%s", ctx.community)
    return _text_reply(event, f"song queue: {link}")


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Parse `!sr`/`!songrequest`/`!sq`/`!songqueue` from chat text; `None` if none match.

    A message starting with `!sr`/`!songrequest` but with a missing/blank
    query gets a usage-hint reply rather than silently doing nothing.
    `!sr status` always answers, flag on or off (module docstring); every
    other subcommand (`set`, a song request) returns `None` when the flag
    is off (or the flag/license server is unreachable), same as an
    unrecognized command. `!sq`/`!songqueue` is a fully separate sibling
    command, checked first, delegated to `_handle_song_queue()` -- see
    module docstring.

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text:
        return None

    if _SQ_PREFIX_RE.match(text):
        logger.debug("social_music_process.sq_matched")
        return await _handle_song_queue(event)

    if not _SR_PREFIX_RE.match(text):
        return None  # not a song request command, skip

    parts = text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    subcommand = query.lower()
    logger.debug("social_music_process.parsed subcommand=%r query=%r", subcommand[:32], query[:64])

    ctx = get_bundle_context()
    enabled = await feature_enabled(
        _FEATURE_FLAG, tenant=ctx.tenant, community=_community_id(ctx.community), default=True
    )
    logger.debug(
        "social_music_process.flag_checked enabled=%s community=%s", enabled, ctx.community
    )

    if subcommand == _STATUS_SUBCOMMAND or subcommand.startswith(f"{_STATUS_SUBCOMMAND} "):
        if not enabled:
            logger.debug("social_music_process.status_disabled_reply")
            return _text_reply(event, _STATUS_DISABLED_REPLY)
        logger.debug("social_music_process.status_routed_to_action")
        return dataclasses.replace(
            event,
            payload={
                **event.payload,
                "text": query,
                _STATUS_CHECK_KEY: True,
                PROCESS_TARGET_APP_ID_KEY: _MUSIC_APP_ID,
            },
        )

    if not enabled:
        logger.debug("social_music_process.flag_disabled_no_reply")
        return None  # feature disabled -- behaves like an unrecognized command

    if subcommand == _SET_SUBCOMMAND or subcommand.startswith(f"{_SET_SUBCOMMAND} "):
        return await _handle_set_subcommand(event, query, ctx)

    if subcommand == _PAUSE_SUBCOMMAND or subcommand.startswith(f"{_PAUSE_SUBCOMMAND} "):
        return await _handle_pause_resume_subcommand(event, _PAUSE_SUBCOMMAND, ctx)

    if subcommand == _RESUME_SUBCOMMAND or subcommand.startswith(f"{_RESUME_SUBCOMMAND} "):
        return await _handle_pause_resume_subcommand(event, _RESUME_SUBCOMMAND, ctx)

    if not query:
        logger.debug("social_music_process.usage_hint_reply")
        return _usage_reply_event(event)

    logger.debug("social_music_process.song_request_routed_to_action")
    return dataclasses.replace(
        event,
        payload={
            **event.payload,
            "text": query,
            "music_query": query,
            PROCESS_TARGET_APP_ID_KEY: _MUSIC_APP_ID,
        },
    )


def _usage_reply_event(event: PlatformEvent) -> PlatformEvent:
    """Build the `!sr`/`!songrequest` usage-hint reply."""
    return _text_reply(event, _SR_USAGE)


async def _handle_set_subcommand(
    event: PlatformEvent, query: str, ctx: BundleContext
) -> PlatformEvent:
    """Route `!sr set <key> <value>` -- only `youtube-labels` is implemented.

    Order: youtube-labels flag + arg presence -> key match -> permission ->
    value presence -> parse. A bare `!sr set` (no key at all), the flag
    being off, or an unimplemented key all reply `_SET_UNAVAILABLE_REPLY`/
    the unknown-setting variant -- neither stamps `PROCESS_TARGET_APP_ID_KEY`.
    Only a successful youtube-labels set/clear routes to the action stage
    (see module docstring for the outgoing event shape and why svc-process
    never calls hub-api directly).
    """
    set_parts = query.split(maxsplit=1)
    set_args = set_parts[1].strip() if len(set_parts) > 1 else ""

    labels_enabled = await feature_enabled(
        _YOUTUBE_LABELS_FEATURE_FLAG,
        tenant=ctx.tenant,
        community=_community_id(ctx.community),
        default=True,
    )
    logger.debug(
        "social_music_process.youtube_labels_flag_checked enabled=%s community=%s",
        labels_enabled,
        ctx.community,
    )
    if not labels_enabled or not set_args:
        logger.debug("social_music_process.set_not_available_reply")
        return _text_reply(event, _SET_UNAVAILABLE_REPLY)

    key_parts = set_args.split(maxsplit=1)
    key_raw = key_parts[0]
    value_raw = key_parts[1].strip() if len(key_parts) > 1 else ""

    if key_raw.lower() != _YOUTUBE_LABELS_KEY_COMMAND:
        logger.debug("social_music_process.set_unknown_key key=%r", key_raw)
        return _text_reply(
            event, f"song requests: unknown setting '{key_raw}' — supported: youtube-labels"
        )

    community_id = _community_id(ctx.community)
    if not await _caller_is_moderator_or_admin(event, community_id):
        logger.debug(
            "social_music_process.set_denied actor=%s community_id=%s", event.actor, community_id
        )
        return _text_reply(event, _SET_PERMISSION_DENIED_REPLY)

    if not value_raw:
        logger.debug("social_music_process.set_youtube_labels_usage_reply")
        return _text_reply(event, _YOUTUBE_LABELS_USAGE_REPLY)

    if value_raw.lower() in _YOUTUBE_LABELS_CLEAR_VALUES:
        labels: list[str] = []
    else:
        parsed = _parse_youtube_labels(value_raw)
        if parsed is None:
            logger.debug("social_music_process.set_labels_over_limit")
            return _text_reply(event, _YOUTUBE_LABELS_LIMIT_REPLY)
        labels = parsed

    logger.debug("social_music_process.set_youtube_labels_routed_to_action count=%d", len(labels))
    return dataclasses.replace(
        event,
        payload={
            **event.payload,
            "subcommand": "set",
            "key": _YOUTUBE_LABELS_PAYLOAD_KEY,
            "value": labels,
            PROCESS_TARGET_APP_ID_KEY: _MUSIC_APP_ID,
        },
    )


async def _handle_pause_resume_subcommand(
    event: PlatformEvent, subcommand: str, ctx: BundleContext
) -> PlatformEvent:
    """Route `!sr pause`/`!sr resume` -- moderator/admin only, no direct reply on success.

    Same permission gate as `!sr set youtube-labels`
    (`_caller_is_moderator_or_admin`) -- a denied caller gets the fixed
    `_PAUSE_RESUME_PERMISSION_DENIED_REPLY`, never routed to the action
    stage. An allowed caller's event is stamped with `subcommand` and
    `PROCESS_TARGET_APP_ID_KEY` only (no `key`/`value`, unlike `set`) and
    routed to `social_music_action`, which performs the actual hub-api
    call and sends the reply -- no direct reply here (module docstring).
    """
    community_id = _community_id(ctx.community)
    if not await _caller_is_moderator_or_admin(event, community_id):
        logger.debug(
            "social_music_process.pause_resume_denied subcommand=%s actor=%s community_id=%s",
            subcommand,
            event.actor,
            community_id,
        )
        return _text_reply(event, _PAUSE_RESUME_PERMISSION_DENIED_REPLY)

    logger.debug("social_music_process.pause_resume_routed_to_action subcommand=%s", subcommand)
    return dataclasses.replace(
        event,
        payload={
            **event.payload,
            "subcommand": subcommand,
            PROCESS_TARGET_APP_ID_KEY: _MUSIC_APP_ID,
        },
    )


def _parse_youtube_labels(value_raw: str) -> list[str] | None:
    """Split/trim/lowercase/dedupe a comma-separated label list; `None` if over any limit.

    Preserves first-seen order. Empty entries (from doubled/leading/
    trailing commas) are dropped rather than rejected.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for raw_label in value_raw.split(","):
        label = raw_label.strip().lower()
        if not label:
            continue
        if len(label) > _MAX_YOUTUBE_LABEL_LEN:
            return None
        if label not in seen:
            seen.add(label)
            labels.append(label)
    if len(labels) > _MAX_YOUTUBE_LABELS:
        return None
    return labels


async def _caller_is_moderator_or_admin(event: PlatformEvent, community_id: int | None) -> bool:
    """Community admin/moderator gate for `!sr set youtube-labels`.

    Same `community_members` lookup convention as `social_alias_process
    ._caller_is_moderator_or_admin` (match by `(platform,
    platform_user_id)` first, else `display_name == event.actor`) --
    replicated locally rather than imported, per this module's own
    dependency convention (see module docstring). Fails closed (denies) on
    a missing community, any lookup error, or no matching row; never
    raises.
    """
    if community_id is None:
        return False

    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_PLATFORM_SQL,
                {
                    "community_id": community_id,
                    "platform": event.platform,
                    "platform_user_id": platform_user_id,
                },
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
        if event.actor:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_DISPLAY_NAME_SQL,
                {"community_id": community_id, "display_name": event.actor},
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
    except Exception as exc:  # noqa: BLE001 -- permission check must fail closed, never crash
        logger.debug("social_music_process.permission_check_failed error=%s", exc)
        return False

    return False
