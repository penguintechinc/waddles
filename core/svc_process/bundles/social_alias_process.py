"""Social alias process bundle -- custom `!<name>` command aliases (product spec, gh #299-adjacent).

Manages `command_aliases` (`config/postgres/migrations/013_schema_optimizations
.sql`, `UNIQUE(community_id, alias)`). Handles alias management only: set, list,
and remove. Bare alias invocations (e.g., `!greet alice`) are expanded centrally
by `bot_process`, not here. Three surfaces:

- `!alias <name> <command> [options...]` -- upsert (positional form; `!alias
  add <name> <command>` is a synonym). Reply: `alias set: !<name> -> !
  <target>`.
- `!unalias <name>` (also `!alias delete <name>` / `!alias remove <name>`) --
  soft-delete. Reply: `alias removed: !<name>` or `no alias named !<name>`.
- bare `!alias` / `!alias list` -- lists active aliases (sorted, capped at
  15 then `...and N more`), or `no aliases set` when empty.

`!unalias` is routed here two ways: directly (this bundle's own standalone
app-bundle activation sees every raw chat message, same as `!alias`/plain
alias invocation) and via `bot_process._FEATURE_MODULES["unalias"]` when the
bot's own app_id dispatches it. `transform()` handles both identically since
dispatch always forwards the untouched original event.

Set/remove require the caller to be a community admin or moderator.
No dedicated permission signal exists in this bundle or any process-stage
sibling today (`community_reputation_process` is read-only, no role gate;
`services/moderation_config.py` gates content categories, not callers) --
this reuses `community_reputation_process._fetch_member`'s own
`community_members` lookup convention (match by `(platform,
platform_user_id)` first, else `display_name == event.actor`, both scoped
to `get_bundle_context().community` -- never `event.payload`, untrusted
platform data) and adds a `role` check against `_ADMIN_ROLES` on top.

Alias-name and first-word-of-expansion validation checks against
`bot_process._BOT_COMMANDS` / `_FEATURE_MODULES` (imported lazily inside
`_known_commands()`, not at module load time -- `bot_process
._load_feature_transforms()` imports THIS module for its own "alias"/
"unalias" dispatch entries, so a top-level `from bundles.bot_process import
...` here would race module-init order depending on which module a caller
imports first). An expansion whose first word is itself an existing alias
is flattened (resolved + stored) rather than stored as a live reference, so
invocation never has to chase a chain.

Gated by the `waddles.bot.command_aliases` PostHog flag (default ON) via
`flask_core.feature_flags.feature_enabled` -- flag off degrades exactly like
an unrecognized command (no reply, DEBUG log only), same convention as
`community_context_process`'s `!cc`.

Every write (`insert`/`update` on `command_aliases`) best-effort-invalidates
the sibling runtime alias-expansion cache via `services.command_alias_store
.invalidate_alias` (landing via a concurrent change to wire `bot_process`'s
own runtime path onto this table) -- imported dynamically via `importlib`
(not a static `from X import Y`, so a missing module never breaks `mypy
--strict` or a write) and degraded to a DEBUG no-op if unavailable.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import logging
import re
from datetime import UTC, datetime

from flask_core import BundleContext, PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.feature_flags import feature_enabled

from bundles._dal_sql import raw_sql_rows

logger = logging.getLogger(__name__)

#: PostHog flag key, `waddles.<module>.<feature>` convention -- default ON,
#: matching `community_context_process._FEATURE_FLAG`'s own rationale (this
#: is core chat functionality, not an opt-in extra).
_FEATURE_FLAG = "waddles.bot.command_aliases"

#: Lowercased alias name: letters, digits, `-`, `_`, 1-32 chars.
_ALIAS_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

#: `community_members.role` values treated as authorized to set/remove
#: aliases -- covers both the original chat-role vocabulary (migration
#: `027_add_streamer_role.sql`: owner/admin/moderator/member/streamer) and
#: the newer web-authz slugs `flask_core.community_access._ADMIN_ROLES`
#: uses (`community-owner`/`community-admin`), since this bundle never
#: knows which vocabulary a given community's rows were seeded with.
_ADMIN_ROLES = frozenset({"owner", "admin", "moderator", "community-owner", "community-admin"})

#: Expansion first-words that would let an alias re-invoke alias management
#: itself -- rejected outright, even though "alias"/"unalias" are otherwise
#: valid `_FEATURE_MODULES` command words (see `_cmd_set_alias`).
_RESERVED_EXPANSION_WORDS = frozenset({"alias", "unalias"})

_MAX_EXPANSION_LEN = 200
_LIST_DISPLAY_LIMIT = 15

_ALIAS_USAGE = (
    "Usage: !alias <name> <command> [options] | !alias add <name> <command> | "
    "!alias list | !alias delete <name> | !unalias <name>"
)
_COMMUNITY_REQUIRED_MSG = (
    "Alias commands require a community context and cannot be used tenant-wide."
)
_PERMISSION_DENIED_MSG = "only moderators/admins can set aliases"
_NO_ALIASES_MSG = "no aliases set — try !alias xx somecommand"
_INVALID_NAME_MSG = "alias names are letters, numbers, - and _ (max 32)"
_TOO_LONG_MSG = "alias expansion is too long (max 200 characters)"

_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND platform = :platform "
    "AND platform_user_id = :platform_user_id LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND display_name = :display_name LIMIT 1"
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Route `!alias ...` and `!unalias ...` management commands only.

    Returns `None` for non-command text, any unrecognized bang-word (bare
    alias invocations are handled by `bot_process`, not here), or when
    `waddles.bot.command_aliases` is off. Raises `ValueError` on a malformed
    event (`text` missing/non-str/blank), same convention as every other
    feature bundle here.
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("event payload missing required 'text' string field")

    text = raw_text.strip()
    if not text.startswith("!"):
        return None

    parts = text[1:].split(maxsplit=1)
    if not parts or not parts[0]:
        return None
    command = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    ctx = get_bundle_context()
    enabled = await feature_enabled(
        _FEATURE_FLAG, tenant=ctx.tenant, community=_community_id(ctx.community), default=True
    )
    if not enabled:
        logger.debug("social_alias_process.flag_disabled command=%s", command)
        return None

    if command == "alias":
        reply_text = await _handle_alias_command(event, rest, ctx)
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})
    if command == "unalias":
        reply_text = await _cmd_remove_alias(event, rest, ctx)
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    logger.debug("social_alias_process.not_mine command=%s", command)
    return None


async def _handle_alias_command(event: PlatformEvent, rest: str, ctx: BundleContext) -> str:
    """Dispatch `!alias ...`'s subcommand: bare/`list`, `delete`/`remove`, `add`, or positional."""
    rest = rest.strip()
    if not rest:
        return await _cmd_list_aliases(ctx)

    sub_parts = rest.split(maxsplit=1)
    sub = sub_parts[0].lower()
    sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""

    if sub == "list":
        return await _cmd_list_aliases(ctx)
    if sub in ("delete", "remove"):
        return await _cmd_remove_alias(event, sub_rest, ctx)
    if sub == "add":
        return await _cmd_set_alias(event, sub_rest, ctx)
    # Positional form: `sub` is itself the alias name, not a subcommand.
    return await _cmd_set_alias(event, rest, ctx)


async def _cmd_set_alias(event: PlatformEvent, args: str, ctx: BundleContext) -> str:
    """Validate, authorize, and upsert `<name> <command> [options...]`.

    Order: usage -> community context -> permission -> name validation ->
    expansion validation (reserved words, known-command-or-flatten, length)
    -> write. See module docstring for the permission-signal provenance and
    the flatten-on-alias-expansion behavior.
    """
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return _ALIAS_USAGE
    name_raw, expansion_raw = parts[0], parts[1].strip()
    alias_name = name_raw.strip().lower()

    if ctx.community is None:
        return _COMMUNITY_REQUIRED_MSG
    community_id = int(ctx.community)

    if not await _caller_is_moderator_or_admin(event, community_id):
        logger.debug(
            "social_alias_process.set_denied actor=%s community_id=%s", event.actor, community_id
        )
        return _PERMISSION_DENIED_MSG

    if not _ALIAS_NAME_RE.match(alias_name):
        logger.debug("social_alias_process.invalid_name name=%r", alias_name)
        return _INVALID_NAME_MSG

    bot_commands, feature_commands = _known_commands()
    if alias_name in bot_commands or alias_name in feature_commands:
        logger.debug("social_alias_process.name_is_builtin name=%s", alias_name)
        return f"!{alias_name} is a built-in command and can't be aliased"

    if not expansion_raw:
        return _ALIAS_USAGE

    expansion_parts = expansion_raw.split(maxsplit=1)
    first_word = expansion_parts[0].lower()
    trailing = expansion_parts[1] if len(expansion_parts) > 1 else ""

    if first_word in _RESERVED_EXPANSION_WORDS:
        logger.debug("social_alias_process.expansion_reserved word=%s", first_word)
        return "an alias can't run !alias"

    try:
        if first_word in bot_commands or first_word in feature_commands:
            target_command = expansion_raw
        else:
            existing_target = await _lookup_alias(community_id, first_word)
            if existing_target is None:
                logger.debug("social_alias_process.unknown_command word=%s", first_word)
                return f"unknown command: {first_word}"
            target_command = (
                f"{existing_target} {trailing}".strip() if trailing else existing_target
            )
            logger.debug(
                "social_alias_process.flattened alias=%s from=%s target=%r",
                alias_name,
                first_word,
                target_command,
            )

        if len(target_command) > _MAX_EXPANSION_LEN:
            return _TOO_LONG_MSG

        await _upsert_alias(community_id, alias_name, target_command, event.actor)
    except Exception as exc:  # noqa: BLE001 -- alias write must reply, never crash the bot
        return f"Failed to set alias: {exc}"

    await _invalidate_alias_cache(community_id=community_id, alias=alias_name)
    return f"alias set: !{alias_name} → !{target_command}"


async def _cmd_remove_alias(event: PlatformEvent, args: str, ctx: BundleContext) -> str:
    """Soft-delete `<name>` (`!unalias`/`!alias delete`/`!alias remove`). Requires admin/mod."""
    alias_name = args.strip().lower()
    if not alias_name:
        return _ALIAS_USAGE

    if ctx.community is None:
        return _COMMUNITY_REQUIRED_MSG
    community_id = int(ctx.community)

    if not await _caller_is_moderator_or_admin(event, community_id):
        logger.debug(
            "social_alias_process.remove_denied actor=%s community_id=%s",
            event.actor,
            community_id,
        )
        return _PERMISSION_DENIED_MSG

    try:
        removed = await _soft_delete_alias(community_id, alias_name)
    except Exception as exc:  # noqa: BLE001 -- alias write must reply, never crash the bot
        return f"Failed to remove alias: {exc}"

    if not removed:
        return f"no alias named !{alias_name}"

    await _invalidate_alias_cache(community_id=community_id, alias=alias_name)
    return f"alias removed: !{alias_name}"


async def _cmd_list_aliases(ctx: BundleContext) -> str:
    """List active aliases for the community, sorted, capped at `_LIST_DISPLAY_LIMIT`."""
    if ctx.community is None:
        return _COMMUNITY_REQUIRED_MSG
    community_id = int(ctx.community)

    try:
        aliases = await _list_aliases(community_id)
    except Exception as exc:  # noqa: BLE001 -- read-only lookup must reply, never crash the bot
        return f"Failed to list aliases: {exc}"

    if not aliases:
        return _NO_ALIASES_MSG

    shown = aliases[:_LIST_DISPLAY_LIMIT]
    body = ", ".join(f"!{name} → !{target}" for name, target in shown)
    if len(aliases) > _LIST_DISPLAY_LIMIT:
        body += f", …and {len(aliases) - _LIST_DISPLAY_LIMIT} more"
    return f"aliases: {body}"


async def _lookup_alias(community_id: int, alias_name: str) -> str | None:
    """Return the active `target_command` for `alias_name`, or `None` if not found."""
    dal = get_bundle_dal()
    query = (
        (dal.command_aliases.community_id == community_id)
        & (dal.command_aliases.alias == alias_name)
        & (dal.command_aliases.deleted_at == None)  # noqa: E711 -- penguin_dal FieldProxy.__eq__(None) => IS NULL
    )
    rows = await dal(query).select()
    row = rows.first()
    return str(row.target_command) if row is not None else None


async def _upsert_alias(
    community_id: int, alias_name: str, target_command: str, created_by: str | None
) -> None:
    """Insert a new alias, or overwrite/revive an existing (possibly soft-deleted) one.

    Matches on `(community_id, alias)` regardless of `deleted_at` -- the
    table's `UNIQUE(community_id, alias)` constraint doesn't exclude
    soft-deleted rows, so `!alias <name> ...` on a previously-removed name
    must UPDATE that row (reviving it), never blind-INSERT into it.
    """
    dal = get_bundle_dal()
    query = (dal.command_aliases.community_id == community_id) & (
        dal.command_aliases.alias == alias_name
    )
    rows = await dal(query).select()
    existing = rows.first()
    if existing is not None:
        await dal(dal.command_aliases.id == existing.id).update(
            target_command=target_command,
            deleted_at=None,
            created_by=created_by or "unknown",
        )
    else:
        await dal.command_aliases.async_insert(
            community_id=community_id,
            alias=alias_name,
            target_command=target_command,
            created_by=created_by or "unknown",
        )


async def _soft_delete_alias(community_id: int, alias_name: str) -> bool:
    """Soft-delete an active alias by name. Returns `False` if none was found."""
    dal = get_bundle_dal()
    query = (
        (dal.command_aliases.community_id == community_id)
        & (dal.command_aliases.alias == alias_name)
        & (dal.command_aliases.deleted_at == None)  # noqa: E711 -- penguin_dal FieldProxy.__eq__(None) => IS NULL
    )
    rows = await dal(query).select()
    row = rows.first()
    if row is None:
        return False
    await dal(dal.command_aliases.id == row.id).update(deleted_at=datetime.now(UTC))
    return True


async def _list_aliases(community_id: int) -> list[tuple[str, str]]:
    """Return `(alias, target_command)` pairs for every active alias, sorted by alias name."""
    dal = get_bundle_dal()
    query = (dal.command_aliases.community_id == community_id) & (
        dal.command_aliases.deleted_at == None  # noqa: E711 -- penguin_dal FieldProxy.__eq__(None) => IS NULL
    )
    rows = await dal(query).select()
    return sorted(
        ((str(row.alias), str(row.target_command)) for row in rows), key=lambda pair: pair[0]
    )


async def _caller_is_moderator_or_admin(event: PlatformEvent, community_id: int) -> bool:
    """Community admin/moderator gate for `!alias`/`!unalias` writes.

    Reuses `community_reputation_process._fetch_member`'s exact
    `community_members` lookup convention (match by `(platform,
    platform_user_id)` first, else `display_name == event.actor`, both
    scoped to `community_id`) since no dedicated permission signal exists
    in this bundle or any process-stage sibling today, then checks `role`
    against `_ADMIN_ROLES`. Fails closed (denies) on any lookup error or
    missing match; never raises.
    """
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
        logger.debug("social_alias_process.permission_check_failed error=%s", exc)
        return False

    return False


async def _invalidate_alias_cache(*, community_id: int, alias: str) -> None:
    """Best-effort call into the sibling runtime alias-expansion cache hook.

    Imported dynamically via `importlib` (same guarded-import shape as
    `bot_process._load_feature_transforms`), not a static `from X import Y`
    -- `services.command_alias_store` is landing via a concurrent sibling
    change to wire `bot_process`'s runtime expansion path onto this same
    table; a static import would also force `mypy --strict` to resolve a
    module that may not exist on disk yet. Missing today, or in any test
    env that doesn't ship it: degrades to a DEBUG no-op, never blocks or
    breaks an alias write.
    """
    try:
        module = importlib.import_module("services.command_alias_store")
        invalidate = module.invalidate_alias
    except (ImportError, AttributeError):
        logger.debug(
            "social_alias_process.invalidate_alias_unavailable community_id=%s alias=%s",
            community_id,
            alias,
        )
        return

    try:
        result = invalidate(community_id=community_id, alias=alias)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 -- cache invalidation must never block/break a write
        logger.debug(
            "social_alias_process.invalidate_alias_failed community_id=%s alias=%s error=%s",
            community_id,
            alias,
            exc,
        )


def _known_commands() -> tuple[frozenset[str], frozenset[str]]:
    """Lazily import `bot_process`'s command vocabulary for name/expansion validation.

    Deferred, not top-level: `bot_process._load_feature_transforms()`
    imports THIS module for its own "alias"/"unalias" dispatch entries, so
    a top-level `from bundles.bot_process import ...` here would race
    module-init order depending on which module a caller imports first.
    """
    from bundles.bot_process import _BOT_COMMANDS, _FEATURE_MODULES

    return _BOT_COMMANDS, frozenset(_FEATURE_MODULES.keys())


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the flag check; `None`/unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None
