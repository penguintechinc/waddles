"""Community loyalty process bundle -- parses `!points`/`!top`/`!shop`/`!redeem` chat commands.

Ports gh #317's process half of the Community Loyalty MVP onto the v3 App
Bundle Feature-contract spine, wired through `bot_process.py`'s
`_FEATURE_MODULES` command router the same way `!sr`/`!songrequest`
delegates to `bundles.social_music_process` -- see that module's own
docstring for the router/dispatch mechanism this bundle reuses unchanged.
All four commands share this module's single `transform()` entrypoint;
the specific command word is parsed back out of `event.payload["text"]`
here (`_dispatch_feature` passes the ORIGINAL event through unmodified).

Commands:
- `!points` -- caller's own balance.
- `!points @user` / `!points user` -- another member's balance;
  moderator/admin only (`_caller_is_moderator_or_admin`).
- `!points add <user> <n>` / `!points remove <user> <n>` -- moderator/
  admin only; `<n>` an integer in `[1, 1_000_000]`.
- `!top` -- the community's top-10 leaderboard.
- `!shop` -- the community's redeemable item catalog.
- `!redeem <sku>` -- spend points on one shop item.

ROUTING (mirrors `bundles.social_music_process`'s identical use of gh
#298's mechanism): every successful parse -- but not a usage-hint/
permission-denied reply -- stamps `PROCESS_TARGET_APP_ID_KEY` onto the
returned event's payload with `_LOYALTY_APP_ID` so `core/svc_process/
runner.py` enqueues onto the loyalty app's `:action` key instead of the
originating bot's. The outgoing payload carries `subcommand`
(`"balance"|"top"|"shop"|"redeem"|"adjust"`) plus whichever of `target`/
`sku`/`delta` that subcommand needs -- `bundles.community_loyalty_action`
(written concurrently) is coded against this exact shape. `target` is the
raw login/id text as typed in chat (leading `@` stripped, case preserved)
-- this bundle never resolves it to a platform-native id itself, same
"pass the typed identifier through" convention already used for `!so
<user>`'s `target` field.

Permission: moderator/admin only for "view another's balance" and "add/
remove" -- same `community_members` role-lookup convention `social_
music_process._caller_is_moderator_or_admin`/`social_alias_process
._caller_is_moderator_or_admin` use (match by `(platform,
platform_user_id)` first, else `display_name == event.actor`, fail
closed), replicated locally per this bundle family's own no-cross-import
convention. `!top`/`!shop`/`!redeem` (spending one's own points) require
no permission check.

Feature-gated via `flask_core.feature_flags.feature_enabled` --
`waddles.community.loyalty` (default ON), the SAME flag key hub-api's own
internal write routes check server-side
(`hub_api/blueprints/v1/community_loyalty.py::FEATURE_COMMUNITY_LOYALTY`).
Flag OFF (or a PostHog/license-server outage, which `feature_enabled`
itself degrades to `default=False` for) means every one of these commands
behaves like an unrecognized command -- no reply.
"""

from __future__ import annotations

import dataclasses
import logging

from flask_core import (
    PROCESS_TARGET_APP_ID_KEY,
    BundleContext,
    PlatformEvent,
    get_bundle_context,
    get_bundle_dal,
)
from flask_core.feature_flags import feature_enabled

from bundles._dal_sql import raw_sql_rows

logger = logging.getLogger(__name__)

#: `app_catalog.app_id` this bundle's action stage is registered under --
#: a successful parse routes to THIS app's `:action` key instead of the
#: originating bot's (module docstring).
_LOYALTY_APP_ID = "waddles.community.loyalty.default"

#: PostHog flag key -- matches hub-api's own `FEATURE_COMMUNITY_LOYALTY`
#: exactly (module docstring). Default ON.
_FEATURE_FLAG = "waddles.community.loyalty"

_POINTS_USAGE_REPLY = "usage: !points [user] | !points add|remove <user> <n>"
_REDEEM_USAGE_REPLY = "usage: !redeem <item>"
_PERMISSION_DENIED_REPLY = "only moderators/admins can adjust points"

_ADD_ACTION = "add"
_REMOVE_ACTION = "remove"
_MIN_DELTA_MAGNITUDE = 1
_MAX_DELTA_MAGNITUDE = 1_000_000

#: `community_members.role` values authorized to view another member's
#: balance / add-remove points -- same vocabulary `social_music_process
#: ._ADMIN_ROLES`/`social_alias_process._ADMIN_ROLES` use.
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


def _text_reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a direct chat reply (no cross-app routing), preserving every other payload field."""
    return dataclasses.replace(event, payload={**event.payload, "text": text})


def _route_to_action(event: PlatformEvent, **extra_payload: object) -> PlatformEvent:
    """Stamp `PROCESS_TARGET_APP_ID_KEY` and merge `extra_payload` -- the successful-parse shape."""
    return dataclasses.replace(
        event,
        payload={**event.payload, **extra_payload, PROCESS_TARGET_APP_ID_KEY: _LOYALTY_APP_ID},
    )


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the flag/permission checks; `None`/unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


def _strip_target(raw: str) -> str:
    """Normalize a typed target -- strip a leading `@`, preserve case (module docstring)."""
    return raw[1:] if raw.startswith("@") else raw


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Parse `!points`/`!top`/`!shop`/`!redeem`; `None` if the text isn't one of these commands.

    `_FEATURE_MODULES` only routes here for these four command words
    (module docstring), so the first word is trusted without a regex
    match -- unlike sibling bundles that support multiple aliases.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text or not text.startswith("!"):
        return None

    parts = text[1:].split(maxsplit=1)
    if not parts:
        return None
    command = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    ctx = get_bundle_context()
    enabled = await feature_enabled(
        _FEATURE_FLAG, tenant=ctx.tenant, community=_community_id(ctx.community), default=True
    )
    logger.debug(
        "community_loyalty_process.flag_checked enabled=%s command=%s community=%s",
        enabled,
        command,
        ctx.community,
    )
    if not enabled:
        logger.debug("community_loyalty_process.flag_disabled_no_reply")
        return None  # feature disabled -- behaves like an unrecognized command

    if command == "points":
        return await _handle_points(event, rest, ctx)
    if command == "top":
        logger.debug("community_loyalty_process.top_routed_to_action")
        return _route_to_action(event, subcommand="top")
    if command == "shop":
        logger.debug("community_loyalty_process.shop_routed_to_action")
        return _route_to_action(event, subcommand="shop")
    if command == "redeem":
        return _handle_redeem(event, rest)

    return None  # not one of this bundle's commands


def _handle_redeem(event: PlatformEvent, rest: str) -> PlatformEvent:
    """`!redeem <sku>` -- own points, no permission gate; first token is the sku, rest ignored."""
    tokens = rest.split()
    if not tokens:
        logger.debug("community_loyalty_process.redeem_usage_reply")
        return _text_reply(event, _REDEEM_USAGE_REPLY)

    sku = tokens[0]
    logger.debug("community_loyalty_process.redeem_routed_to_action sku=%r", sku)
    return _route_to_action(event, subcommand="redeem", sku=sku)


async def _handle_points(event: PlatformEvent, rest: str, ctx: BundleContext) -> PlatformEvent:
    """`!points` (own balance), `!points <user>` / `add|remove <user> <n>` (mod/admin only)."""
    if not rest:
        logger.debug("community_loyalty_process.points_own_balance_routed_to_action")
        return _route_to_action(event, subcommand="balance")

    tokens = rest.split()
    first = tokens[0].lower()

    if first in (_ADD_ACTION, _REMOVE_ACTION):
        return await _handle_points_adjust(event, first, tokens, ctx)

    # `!points <user>` -- view another member's balance, moderator/admin only.
    community_id = _community_id(ctx.community)
    if not await _caller_is_moderator_or_admin(event, community_id):
        logger.debug(
            "community_loyalty_process.balance_view_denied actor=%s community_id=%s",
            event.actor,
            community_id,
        )
        return _text_reply(event, _PERMISSION_DENIED_REPLY)

    target = _strip_target(tokens[0])
    logger.debug("community_loyalty_process.balance_view_routed_to_action target=%r", target)
    return _route_to_action(event, subcommand="balance", target=target)


async def _handle_points_adjust(
    event: PlatformEvent, action: str, tokens: list[str], ctx: BundleContext
) -> PlatformEvent:
    """`!points add|remove <user> <n>` -- moderator/admin only; `n` in `[1, 1_000_000]`."""
    if len(tokens) != 3:
        logger.debug("community_loyalty_process.adjust_usage_reply action=%s", action)
        return _text_reply(event, _POINTS_USAGE_REPLY)

    community_id = _community_id(ctx.community)
    if not await _caller_is_moderator_or_admin(event, community_id):
        logger.debug(
            "community_loyalty_process.adjust_denied action=%s actor=%s community_id=%s",
            action,
            event.actor,
            community_id,
        )
        return _text_reply(event, _PERMISSION_DENIED_REPLY)

    target = _strip_target(tokens[1])
    magnitude_raw = tokens[2]
    if not magnitude_raw.isdigit():
        logger.debug("community_loyalty_process.adjust_invalid_amount value=%r", magnitude_raw)
        return _text_reply(event, _POINTS_USAGE_REPLY)

    magnitude = int(magnitude_raw)
    if not (_MIN_DELTA_MAGNITUDE <= magnitude <= _MAX_DELTA_MAGNITUDE):
        logger.debug("community_loyalty_process.adjust_amount_out_of_range value=%d", magnitude)
        return _text_reply(event, _POINTS_USAGE_REPLY)

    delta = magnitude if action == _ADD_ACTION else -magnitude
    logger.debug(
        "community_loyalty_process.adjust_routed_to_action target=%r delta=%d", target, delta
    )
    return _route_to_action(event, subcommand="adjust", target=target, delta=delta)


async def _caller_is_moderator_or_admin(event: PlatformEvent, community_id: int | None) -> bool:
    """Community admin/moderator gate for viewing another's balance / `!points add|remove`.

    Same `community_members` lookup convention as `social_music_process
    ._caller_is_moderator_or_admin` (match by `(platform,
    platform_user_id)` first, else `display_name == event.actor`) --
    replicated locally rather than imported (module docstring). Fails
    closed (denies) on a missing community, any lookup error, or no
    matching row; never raises.
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
        logger.debug("community_loyalty_process.permission_check_failed error=%s", exc)
        return False

    return False
