"""Community context process handler -- `!cc` community-context switch (gh #311).

Some channels are linked to several communities at once (`community_servers`,
one `is_primary` default per channel). `!cc` lets a user check or switch
which of those linked communities their own subsequent commands (e.g.
`!poll`, `!quote`) resolve against, via a per-user, per-channel 24h override
stored by the sibling `services.community_context_store` module
(`user_platform_context`, TTL-backed). This handler never implements that
storage itself -- it only calls the four functions that module exposes
(`list_channel_communities`, `get_context`, `set_context`, `clear_context`)
and formats the chat reply, same separation-of-concerns as
`community_reputation_process` calling `get_bundle_dal()` directly instead
of owning its own query layer.

Three subcommands:
- `!cc` (bare) -- reports the requester's current resolved community (their
  active override, or the channel's `is_primary` default) plus the other
  communities linked to this channel.
- `!cc <name>` -- switches the requester's override to the named community
  (case-insensitive, spaces/hyphens treated as equal) for 24h.
- `!cc default` / `!cc reset` -- clears the override, falling back to the
  channel's `is_primary` community.

A channel with zero linked communities, or exactly one (bare `!cc` only),
gets a distinct reply rather than the general multi-community format -- see
`transform()` and `_reply_status()`.

Identity is read straight from the inbound event, same convention as
`social_welcome_process`: `event.platform`, `event.payload["author_id"]`
(platform-native user id -- already a tokenized, opaque platform identifier
set upstream by ingest, never raw PII), and `event.payload["channel_id"]`
falling back to `event.payload["channel_name"]` -- the same
`channel_id or channel_name` resolution `svc_process/runner.py`'s
`_emit_activity()` uses to normalize Discord's `channel_id` against
Twitch's `channel_name` into one platform-entity identifier. Never read
from `get_bundle_context()` for this -- channel/user identity is per-event,
not per-envelope, unlike the tenant/community scope
`community_reputation_process` reads from there.

Feature-gated (default ON in alpha) via `flask_core.feature_flags.
feature_enabled` -- `waddles.community.context`. Flag OFF (or a
PostHog/license-server outage, which `feature_enabled` itself degrades to
`default=True` for here) means `!cc` behaves like an unrecognized command
(no reply, DEBUG log only) -- unlike `!sr status`, no subcommand of `!cc`
answers while the flag is off.

Read/write against the sibling store module never crashes the bot: any
exception from `list_channel_communities`/`get_context`/`set_context`/
`clear_context` is caught, logged, and turned into a short graceful reply
-- defense in depth matching `community_reputation_process`'s own guard,
since this handler (like that one) is reachable directly, not just via
`bot_process._dispatch_feature`'s own guarded dispatch.
"""

from __future__ import annotations

import dataclasses
import logging

from flask_core import PlatformEvent, get_bundle_context
from flask_core.feature_flags import feature_enabled

from services.community_context_store import (
    ChannelCommunity,
    clear_context,
    get_context,
    list_channel_communities,
    set_context,
)

logger = logging.getLogger(__name__)

_COMMAND_WORD = "cc"

#: PostHog flag key, `waddles.<module>.<feature>` convention (see
#: `waddles.social.music`/`waddles.community.forums` elsewhere in this
#: repo). Default ON for the alpha board demo -- see module docstring.
_FEATURE_FLAG = "waddles.community.context"

#: `!cc default` / `!cc reset` -- both clear the per-user override.
_RESET_SUBCOMMANDS = frozenset({"default", "reset"})

#: Per-user, per-channel override TTL -- matches `services.
#: community_context_store.set_context`'s own default, passed explicitly
#: so this handler's intent is visible without reading that module.
_CONTEXT_TTL_S = 86400

_NO_LINKED_COMMUNITIES_REPLY = "this channel isn't linked to any community yet"
_GUARD_REPLY = "community context lookup is unavailable right now -- try again in a bit! \U0001f427"


def _text_reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a direct chat reply, preserving every other payload field."""
    return dataclasses.replace(event, payload={**event.payload, "text": text})


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the flag check; `None`/unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


def _normalize(name: str) -> str:
    """Fold a community name for matching -- lowercase, hyphens as spaces, whitespace collapsed."""
    return " ".join(name.strip().lower().replace("-", " ").split())


def _find_by_name(communities: list[ChannelCommunity], name: str) -> ChannelCommunity | None:
    """Case/hyphen/space-insensitive lookup of `name` among this channel's linked communities."""
    target = _normalize(name)
    for community in communities:
        if _normalize(community.name) == target:
            return community
    return None


def _by_id(communities: list[ChannelCommunity], community_id: int) -> ChannelCommunity | None:
    """Look up a linked community by id, or `None` if it's no longer linked to this channel."""
    for community in communities:
        if community.id == community_id:
            return community
    return None


def _primary(communities: list[ChannelCommunity]) -> ChannelCommunity:
    """Return the channel's `is_primary` community, defaulting to the first entry.

    The fallback is defensive only -- `community_servers` enforces one
    `is_primary` default per channel; this covers a data anomaly rather
    than the normal path. Caller guarantees `communities` is non-empty.
    """
    for community in communities:
        if community.is_primary:
            return community
    return communities[0]


async def _reply_status(
    event: PlatformEvent,
    *,
    platform_user_id: str,
    platform_entity_id: str,
    communities: list[ChannelCommunity],
) -> PlatformEvent:
    """Build the bare `!cc` reply -- current community, default marker, and siblings."""
    if len(communities) == 1:
        only = communities[0]
        logger.debug("community_context_process.status_single_community id=%d", only.id)
        return _text_reply(
            event, f"community context: {only.name} (only community on this channel)"
        )

    primary = _primary(communities)
    try:
        override_id = await get_context(
            platform=event.platform,
            platform_user_id=platform_user_id,
            platform_entity_id=platform_entity_id,
        )
    except Exception as exc:  # noqa: BLE001 -- lookup failure must never crash the bot
        logger.error("community_context_process.get_context_failed error=%s", exc)
        return _text_reply(event, _GUARD_REPLY)

    current = _by_id(communities, override_id) if override_id is not None else None
    if current is None:
        current = primary
    logger.debug(
        "community_context_process.status_current id=%d is_default=%s",
        current.id,
        current.id == primary.id,
    )

    suffix = " (default)" if current.id == primary.id else ""
    others = ", ".join(community.name for community in communities if community.id != current.id)
    return _text_reply(event, f"community context: {current.name}{suffix} — available: {others}")


async def _reply_reset(
    event: PlatformEvent,
    *,
    platform_user_id: str,
    platform_entity_id: str,
    communities: list[ChannelCommunity],
) -> PlatformEvent:
    """Clear the per-user override for `!cc default`/`!cc reset`, replying with the default."""
    primary = _primary(communities)
    try:
        await clear_context(
            platform=event.platform,
            platform_user_id=platform_user_id,
            platform_entity_id=platform_entity_id,
        )
    except Exception as exc:  # noqa: BLE001 -- lookup failure must never crash the bot
        logger.error("community_context_process.clear_context_failed error=%s", exc)
        return _text_reply(event, _GUARD_REPLY)
    logger.debug("community_context_process.context_reset community_id=%d", primary.id)
    return _text_reply(event, f"community context reset to {primary.name} (default)")


async def _reply_switch(
    event: PlatformEvent,
    name: str,
    *,
    platform_user_id: str,
    platform_entity_id: str,
    communities: list[ChannelCommunity],
) -> PlatformEvent:
    """Switch the per-user override to the named community for `!cc <name>`, or report no match."""
    match = _find_by_name(communities, name)
    if match is None:
        names = ", ".join(community.name for community in communities)
        logger.debug("community_context_process.switch_no_match name=%r", name)
        return _text_reply(event, f"no community named '{name}' on this channel — try: {names}")

    try:
        await set_context(
            platform=event.platform,
            platform_user_id=platform_user_id,
            platform_entity_id=platform_entity_id,
            community_id=match.id,
            ttl_s=_CONTEXT_TTL_S,
        )
    except Exception as exc:  # noqa: BLE001 -- lookup failure must never crash the bot
        logger.error("community_context_process.set_context_failed error=%s", exc)
        return _text_reply(event, _GUARD_REPLY)
    logger.debug("community_context_process.context_switched community_id=%d", match.id)
    return _text_reply(event, f"switched to {match.name} for 24h")


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Parse `!cc` from chat text; return `None` for non-matching messages.

    Reports/switches the requester's community-context override for
    channels linked to more than one community -- see module docstring for
    the three subcommand shapes and the zero/one-community edge replies.

    Raises `ValueError` on a malformed event (missing `text`, `author_id`,
    or both `channel_id`/`channel_name`) -- the process runner catches this
    per-event so one bad event never kills the poll loop.
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = raw_text.strip()
    if not text.startswith("!"):
        return None
    parts = text[1:].split(maxsplit=1)
    if not parts or parts[0].lower() != _COMMAND_WORD:
        return None  # not a community-context command, skip
    arg = parts[1].strip() if len(parts) > 1 else ""
    logger.debug("community_context_process.parsed arg=%r", arg[:64])

    ctx = get_bundle_context()
    enabled = await feature_enabled(
        _FEATURE_FLAG, tenant=ctx.tenant, community=_community_id(ctx.community), default=True
    )
    logger.debug(
        "community_context_process.flag_checked enabled=%s community=%s", enabled, ctx.community
    )
    if not enabled:
        logger.debug("community_context_process.flag_disabled_no_reply")
        return None  # feature disabled -- behaves like an unrecognized command

    raw_author_id = event.payload.get("author_id")
    if not raw_author_id or not isinstance(raw_author_id, str):
        raise ValueError("event payload missing required 'author_id' string field")
    platform_user_id = raw_author_id

    raw_entity_id = event.payload.get("channel_id") or event.payload.get("channel_name")
    if not raw_entity_id or not isinstance(raw_entity_id, str):
        raise ValueError("event payload missing required 'channel_id'/'channel_name' string field")
    platform_entity_id = raw_entity_id

    try:
        communities = await list_channel_communities(
            platform=event.platform, platform_entity_id=platform_entity_id
        )
    except Exception as exc:  # noqa: BLE001 -- lookup failure must never crash the bot
        logger.error("community_context_process.list_communities_failed error=%s", exc)
        return _text_reply(event, _GUARD_REPLY)
    logger.debug("community_context_process.communities_loaded count=%d", len(communities))

    if not communities:
        logger.debug("community_context_process.no_linked_communities")
        return _text_reply(event, _NO_LINKED_COMMUNITIES_REPLY)

    if not arg:
        return await _reply_status(
            event,
            platform_user_id=platform_user_id,
            platform_entity_id=platform_entity_id,
            communities=communities,
        )

    if arg.lower() in _RESET_SUBCOMMANDS:
        return await _reply_reset(
            event,
            platform_user_id=platform_user_id,
            platform_entity_id=platform_entity_id,
            communities=communities,
        )

    return await _reply_switch(
        event,
        arg,
        platform_user_id=platform_user_id,
        platform_entity_id=platform_entity_id,
        communities=communities,
    )
