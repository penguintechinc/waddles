"""Social shoutout process bundle -- parses `!so`/`!shoutout`/`!vso` chat commands.

Normalizes a chat shoutout command into a structured `PlatformEvent` for
the action stage (`bundles.twitch_shoutout_action`, written concurrently
against this bundle's payload contract -- see below), which performs the
actual shoutout (chat message and/or Twitch clip/video) and replies in
place.

Ports gh #316's process half of the legacy `action/interactive/
shoutout_interaction_module` (`!so <user>` text shoutout, `!vso <user>`
video shoutout, per-community `so_permission`/`vso_permission`), rewired
onto the v3 App Bundle Feature-contract spine (`libs/bot_module/
features.py`'s `bot.shoutout` Feature, flag `waddles.bot.shoutout`,
default App `waddles.bot.shoutout.default`) instead of that module's own
standalone Flask service.

Supports three command words, two of which share one payload `kind`:
- `!so <user>` / `!shoutout <user>` -- text shoutout (`kind="text"`)
- `!vso <user>` -- video/clip shoutout (`kind="video"`)

`<user>` is normalized (leading `@` stripped, lowercased) and validated
against Twitch's login character set (`^[a-z0-9_]{3,25}$`, post-
normalization) -- shoutouts target a Twitch channel regardless of which
platform (Discord or Twitch) the command was issued from, same as the
legacy module's `identity_service`/`twitch_service` resolution target.

ROUTING (mirrors `bundles.community_forums_process`'s gh #298 mechanism
and `bundles.social_music_process`'s identical use of it): a successful
parse -- but not a usage-hint/invalid-login/permission-denied/self-
shoutout reply -- stamps `PROCESS_TARGET_APP_ID_KEY` onto the returned
event's payload with `_SHOUTOUT_APP_ID`. `bot_process.py` delegates
`!so`/`!shoutout`/`!vso` to this bundle's `transform()` in-process and
returns whatever it gets back unmodified, so this key rides all the way
to `core/svc_process/runner.py`, which enqueues the event onto the
shoutout app's `:action` key instead of the originating bot's -- see
`PROCESS_TARGET_APP_ID_KEY`'s docstring in `flask_core.stream_pipeline`
for the full mechanism. The outgoing payload carries `subcommand=
"shoutout"`, `kind` (`"text"`/`"video"`), and `target` (the normalized
login) -- `twitch_shoutout_action` is written against this exact shape.

PERMISSION: `shoutout_config.so_permission`/`vso_permission`
(`config/postgres/migrations/046_add_remaining_admin_tables.sql:73-118`,
same table `hub_api/services/bot_shoutout.py` serves the admin UI from)
gates who may issue each command -- one of `admin_only`, `mod`, `vip`,
`subscriber`, `everyone`. Read directly here via `penguin_dal`'s
`raw_sql_rows()` escape hatch (D21a; `flask_core.bundle_runtime`, same raw-SQL
access model as `services/moderation_config.py`/`services/
community_context_store.py`); no reader of `shoutout_config` exists
anywhere in svc-process today, so a missing table/row/connection error all
degrade to the column's own default, `"mod"` (`_shoutout_permission`),
logged at DEBUG rather than raised or denied outright.

Caller role evaluation replicates `social_music_process.
_caller_is_moderator_or_admin`'s `community_members` lookup convention
(match by `(platform, platform_user_id)` first, else `display_name ==
event.actor`; replicated locally rather than imported, per this bundle
family's own dependency convention) but returns the raw role string
rather than a boolean, since `admin_only` and `mod` are different
thresholds. `vip`/`subscriber` cannot be evaluated: neither the Discord
nor the Twitch ingest bundle currently carries badge/subscription data on
`PlatformEvent.payload` (`core/svc_ingest/bundles/twitch_ingest.py`'s own
module docstring: `is_mod`/`is_subscriber`/`is_broadcaster` are a
"documented gap, not silently dropped" since the `waddle_transports`
realignment) -- both permission levels degrade to `everyone` (always
satisfied) rather than failing closed, a deliberate, documented call
(gh #316) pending real badge data reaching the pipeline.

Feature-gated (default ON) via `flask_core.feature_flags.feature_enabled`
-- `waddles.bot.shoutout`, matching `libs/bot_module/features.py`'s
`bot.shoutout` Feature contract's own `flag` field. Flag OFF (or a
PostHog/license-server outage, which `feature_enabled` itself degrades to
`default=True` for) means no reply at all -- behaves like an unrecognized
command, checked before any other validation so a disabled feature never
pays a DB round trip.

Self-shoutout (`target == caller`, both normalized) is rejected
regardless of permission level -- checked before the permission/role DB
lookups since it needs no DB access and is a flat rule, not a threshold.
"""

from __future__ import annotations

import dataclasses
import logging
import re

from flask_core import PROCESS_TARGET_APP_ID_KEY, PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows
from flask_core.feature_flags import feature_enabled

logger = logging.getLogger(__name__)

#: Matches `!so`/`!shoutout`, either alias -- text shoutout.
_SO_PREFIX_RE = re.compile(r"^!(so|shoutout)\b", re.IGNORECASE)
#: Matches `!vso` -- video/clip shoutout.
_VSO_PREFIX_RE = re.compile(r"^!vso\b", re.IGNORECASE)

#: Post-normalization (lowercased, leading `@` stripped) Twitch login
#: character set -- same shape `identity_service.py`/`twitch_service.py`
#: validated in the legacy module.
_LOGIN_RE = re.compile(r"^[a-z0-9_]{3,25}$")

_SO_USAGE = "usage: !so <user>"
_VSO_USAGE = "usage: !vso <user>"
_INVALID_LOGIN_REPLY = "that doesn't look like a Twitch username"
_SELF_SHOUTOUT_REPLY = "you can't shout yourself out"
_PERMISSION_DENIED_REPLY = "you don't have permission to shout out"

#: `app_catalog.app_id` this bundle's action stage is registered under
#: (`libs/bot_module/features.py`'s `bot.shoutout` default App). A
#: successful parse routes to THIS app's `:action` key instead of the
#: originating bot's -- see module docstring.
_SHOUTOUT_APP_ID = "waddles.bot.shoutout.default"

#: PostHog flag key gating `!so`/`!shoutout`/`!vso` entirely -- matches
#: `libs/bot_module/features.py`'s `bot.shoutout` Feature contract `flag`.
#: Default ON.
_FEATURE_FLAG = "waddles.bot.shoutout"

#: `shoutout_config.so_permission`/`vso_permission`'s own column default
#: (migration 046) -- used whenever the table/row/connection isn't
#: reachable, never raised or treated as a denial.
_DEFAULT_PERMISSION = "mod"

#: Permission levels this bundle can never evaluate for lack of badge
#: data (`vip`/`subscriber`) plus the always-open `everyone` -- all three
#: are satisfied unconditionally. See module docstring.
_ALWAYS_ALLOWED_PERMISSIONS = frozenset({"everyone", "vip", "subscriber"})

#: `community_members.role` values satisfying `admin_only` -- owner/admin
#: tiers only, NOT moderator.
_ADMIN_ONLY_ROLES = frozenset({"owner", "admin", "community-owner", "community-admin"})

#: `community_members.role` values satisfying `mod` -- moderator or
#: above. Same vocabulary `social_music_process._ADMIN_ROLES` uses.
_MOD_OR_ABOVE_ROLES = frozenset(
    {"owner", "admin", "moderator", "community-owner", "community-admin"}
)

_SHOUTOUT_CONFIG_SQL = (
    "SELECT so_permission, vso_permission FROM shoutout_config "
    "WHERE community_id = :community_id LIMIT 1"
)
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND platform = :platform "
    "AND platform_user_id = :platform_user_id LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND display_name = :display_name LIMIT 1"
)


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the flag/config/role lookups; unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


def _text_reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a direct chat reply (no cross-app routing), preserving every other payload field."""
    return dataclasses.replace(event, payload={**event.payload, "text": text})


def _normalize_login(raw: str) -> str:
    """Strip a leading `@` and lowercase -- shared by target parsing and self-shoutout check."""
    return raw.strip().lstrip("@").lower()


async def _shoutout_permission(community_id: int | None, kind: str) -> str:
    """Read `so_permission`/`vso_permission` from `shoutout_config`; default on any miss.

    Degrades to `_DEFAULT_PERMISSION` (`"mod"`, the column's own default)
    on a missing community, a missing/absent row, or any DB error -- never
    raises, never denies outright on an infrastructure problem. See module
    docstring.
    """
    if community_id is None:
        logger.debug("social_shoutout_process.permission_default_no_community kind=%s", kind)
        return _DEFAULT_PERMISSION

    column = "so_permission" if kind == "text" else "vso_permission"
    try:
        dal = get_bundle_dal()
        rows = await raw_sql_rows(dal, _SHOUTOUT_CONFIG_SQL, {"community_id": community_id})
    except Exception as exc:  # noqa: BLE001 -- must never block a shoutout, only degrade
        logger.debug("social_shoutout_process.permission_lookup_failed error=%s", exc)
        return _DEFAULT_PERMISSION

    if not rows:
        logger.debug(
            "social_shoutout_process.permission_default_no_config community_id=%s", community_id
        )
        return _DEFAULT_PERMISSION

    value = rows[0][column]
    return str(value) if value else _DEFAULT_PERMISSION


async def _caller_role(event: PlatformEvent, community_id: int | None) -> str | None:
    """`community_members.role` for the caller, or `None` on any miss/error.

    Same lookup convention as `social_music_process.
    _caller_is_moderator_or_admin` (match by `(platform,
    platform_user_id)` first, else `display_name == event.actor`) --
    replicated locally rather than imported (see module docstring). Fails
    closed (`None`) on a missing community, any lookup error, or no
    matching row; never raises.
    """
    if community_id is None:
        return None

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
                return str(rows[0]["role"]).lower()
        if event.actor:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_DISPLAY_NAME_SQL,
                {"community_id": community_id, "display_name": event.actor},
            )
            if rows:
                return str(rows[0]["role"]).lower()
    except Exception as exc:  # noqa: BLE001 -- permission check must fail closed, never crash
        logger.debug("social_shoutout_process.role_lookup_failed error=%s", exc)
        return None

    return None


def _permission_satisfied(permission: str, role: str | None) -> bool:
    """Evaluate a `so_permission`/`vso_permission` value against the caller's community role.

    `everyone`/`vip`/`subscriber` are all always-satisfied (see module
    docstring on the `vip`/`subscriber` badge-data gap); `admin_only`
    requires owner/admin; `mod` requires moderator or above. An
    unrecognized value (defensive only -- the DB column is otherwise
    constrained) falls back to the `mod` threshold rather than either
    extreme.
    """
    normalized = permission.lower()
    if normalized in _ALWAYS_ALLOWED_PERMISSIONS:
        return True
    if normalized == "admin_only":
        return role in _ADMIN_ONLY_ROLES
    if normalized == "mod":
        return role in _MOD_OR_ABOVE_ROLES
    logger.debug("social_shoutout_process.unknown_permission_value permission=%s", normalized)
    return role in _MOD_OR_ABOVE_ROLES


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Parse `!so`/`!shoutout`/`!vso` from chat text; `None` if none match.

    Order: prefix match -> feature flag (off -> `None`, no DB touched) ->
    missing target -> invalid login format -> self-shoutout -> permission
    (config read + role lookup). Only a fully successful parse stamps
    `PROCESS_TARGET_APP_ID_KEY`; every other reply (usage hint, invalid
    login, self-shoutout, permission denied) is a plain chat reply that
    stays on the originating bot's own action key.

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text:
        return None

    if _VSO_PREFIX_RE.match(text):
        kind = "video"
        usage = _VSO_USAGE
    elif _SO_PREFIX_RE.match(text):
        kind = "text"
        usage = _SO_USAGE
    else:
        return None  # not a shoutout command, skip

    ctx = get_bundle_context()
    community_id = _community_id(ctx.community)
    enabled = await feature_enabled(
        _FEATURE_FLAG, tenant=ctx.tenant, community=community_id, default=True
    )
    logger.debug(
        "social_shoutout_process.flag_checked enabled=%s kind=%s community=%s",
        enabled,
        kind,
        ctx.community,
    )
    if not enabled:
        logger.debug("social_shoutout_process.flag_disabled_no_reply")
        return None  # feature disabled -- behaves like an unrecognized command

    parts = text.split(maxsplit=1)
    raw_target = parts[1].strip() if len(parts) > 1 else ""
    if not raw_target:
        logger.debug("social_shoutout_process.usage_hint_reply kind=%s", kind)
        return _text_reply(event, usage)

    target = _normalize_login(raw_target)
    if not _LOGIN_RE.match(target):
        logger.debug("social_shoutout_process.invalid_login_reply kind=%s target=%r", kind, target)
        return _text_reply(event, _INVALID_LOGIN_REPLY)

    caller = _normalize_login(event.actor) if event.actor else ""
    if caller and target == caller:
        logger.debug("social_shoutout_process.self_shoutout_denied actor=%s", event.actor)
        return _text_reply(event, _SELF_SHOUTOUT_REPLY)

    permission = await _shoutout_permission(community_id, kind)
    role = await _caller_role(event, community_id)
    if not _permission_satisfied(permission, role):
        logger.debug(
            "social_shoutout_process.permission_denied kind=%s permission=%s role=%s",
            kind,
            permission,
            role,
        )
        return _text_reply(event, _PERMISSION_DENIED_REPLY)

    logger.debug("social_shoutout_process.routed_to_action kind=%s target=%s", kind, target)
    return dataclasses.replace(
        event,
        payload={
            **event.payload,
            "subcommand": "shoutout",
            "kind": kind,
            "target": target,
            PROCESS_TARGET_APP_ID_KEY: _SHOUTOUT_APP_ID,
        },
    )
