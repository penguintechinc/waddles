"""Shared table bindings + tenant gate for the Community-module port (M6).

`app.py` is frozen (`routers/_discovery.py`'s auto-discovery contract:
port agents never edit `app.py`/`routers/*.py`/`blueprints/__init__.py`),
so this module cannot register its pydal tables at startup the way
`_bind_reference_tables` binds `tenants` there, or the way
`flask_core.app_bundle_tables.init_app_bundle_tables` documents ("call
once per process during service startup"). Instead every Community
blueprint handler calls `ensure_community_tables(dal)` first -- an
idempotent, near-zero-cost membership check (`dal.tables` is a plain
list) that only calls `dal.define_table(...)` the first time a given
table name is missing. All tables use `migrate=False`: schema already
exists (migrations 000/004/014/044/057/058/065), pydal only maps onto it
(security.md-adjacent: this process never owns DDL).

Query style matches this repo's established pattern (`services/core-
community/app.py`, `blueprints/v2/platform.py`) rather than
`backend-python.md`'s general `asyncio.to_thread` guidance: the raw
`pydal` `dal` (not `AsyncDAL`) is called synchronously from inside async
handlers, and every write is followed by an explicit `dal.commit()` (no
autocommit -- see `services/core-community/app.py`). Complex
aggregations/upserts/window functions the pydal query builder can't
express go through `dal.executesql(sql, placeholders=[...])` with `$1..`
placeholders, mirroring `action/interactive/clip_interaction_module/
services/clip_service.py`.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from flask_core.auth import verify_service_key
from flask_core.tenancy import TenantContext


def ensure_community_tables(dal: Any, *, migrate: bool = False) -> None:
    """Idempotently define every table the Community-module port touches.

    Safe to call at the top of every request handler -- `dal.tables` is a
    plain Python list, so the guard is a cheap membership check, and a
    second `define_table` call for an already-defined name would raise.

    `migrate` defaults to `False` (production: schema owned by SQL
    migrations, pydal only maps onto it). Tests pass `migrate=True`
    against a throwaway `sqlite:memory` DAL so the same field
    definitions double as the test schema -- mirrors `tests/conftest.py`'s
    `tenant_db` fixture, one definition instead of two schemas drifting.
    """
    if "communities" not in dal.tables:
        # Minimal projection -- schema owned by 000_create_base_schema.sql
        # + 058_tenants_and_claims.sql (tenant_id added there). Only the
        # columns `tenant_scoped()`/`community_in_tenant()` need.
        dal.define_table(
            "communities",
            dal.Field("name", "string"),
            dal.Field("tenant_id", "integer"),
            migrate=migrate,
        )

    if "hub_users" not in dal.tables:
        dal.define_table(
            "hub_users",
            dal.Field("username", "string"),
            dal.Field("display_name", "string"),
            dal.Field("email", "string"),
            dal.Field("avatar_url", "text"),
            migrate=migrate,
        )

    if "hub_chat_messages" not in dal.tables:
        dal.define_table(
            "hub_chat_messages",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("channel_name", "string"),
            dal.Field("sender_hub_user_id", "integer"),
            dal.Field("sender_platform", "string"),
            dal.Field("sender_username", "string"),
            dal.Field("sender_avatar_url", "text"),
            dal.Field("message_content", "text", notnull=True),
            dal.Field("message_type", "string", default="text"),
            dal.Field("hub_channel_id", "integer"),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "activity_watch_sessions" not in dal.tables:
        dal.define_table(
            "activity_watch_sessions",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("hub_user_id", "integer"),
            dal.Field("platform", "string", notnull=True),
            dal.Field("platform_user_id", "string", notnull=True),
            dal.Field("platform_username", "string"),
            dal.Field("channel_id", "string", notnull=True),
            dal.Field("session_start", "datetime"),
            dal.Field("session_end", "datetime"),
            dal.Field("duration_seconds", "integer", default=0),
            dal.Field("is_active", "boolean", default=True),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "activity_message_events" not in dal.tables:
        dal.define_table(
            "activity_message_events",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("hub_user_id", "integer"),
            dal.Field("platform", "string", notnull=True),
            dal.Field("platform_user_id", "string", notnull=True),
            dal.Field("platform_username", "string"),
            dal.Field("channel_id", "string"),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "activity_stats_daily" not in dal.tables:
        dal.define_table(
            "activity_stats_daily",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("hub_user_id", "integer"),
            dal.Field("platform_user_id", "string"),
            dal.Field("platform_username", "string"),
            dal.Field("stat_date", "date"),
            dal.Field("watch_time_seconds", "integer", default=0),
            dal.Field("message_count", "integer", default=0),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "community_leaderboard_config" not in dal.tables:
        dal.define_table(
            "community_leaderboard_config",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("enabled_platforms", "json"),
            dal.Field("watch_time_enabled", "boolean", default=True),
            dal.Field("messages_enabled", "boolean", default=True),
            dal.Field("public_leaderboard", "boolean", default=True),
            dal.Field("min_watch_time_minutes", "integer", default=60),
            dal.Field("min_message_count", "integer", default=10),
            dal.Field("display_limit", "integer", default=10),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "announcements" not in dal.tables:
        dal.define_table(
            "announcements",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("title", "string", notnull=True),
            dal.Field("content", "text", notnull=True),
            dal.Field("announcement_type", "string", default="general"),
            dal.Field("status", "string", default="published"),
            dal.Field("is_pinned", "boolean", default=False),
            dal.Field("created_by", "integer"),
            dal.Field("created_by_name", "string"),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_by", "integer"),
            dal.Field("updated_at", "datetime"),
            dal.Field("published_at", "datetime"),
            dal.Field("archived_at", "datetime"),
            migrate=migrate,
        )

    if "announcement_broadcasts" not in dal.tables:
        dal.define_table(
            "announcement_broadcasts",
            dal.Field("announcement_id", "integer", notnull=True),
            dal.Field("community_server_id", "integer"),
            dal.Field("platform", "string", notnull=True),
            dal.Field("channel_id", "string"),
            dal.Field("status", "string", default="pending"),
            dal.Field("platform_message_id", "string"),
            dal.Field("error_message", "text"),
            dal.Field("broadcast_at", "datetime"),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "community_servers" not in dal.tables:
        dal.define_table(
            "community_servers",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("platform", "string", notnull=True),
            dal.Field("platform_server_id", "string", notnull=True),
            dal.Field("platform_server_name", "string"),
            dal.Field("status", "string", default="pending"),
            dal.Field("config", "json"),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "community_server_channels" not in dal.tables:
        dal.define_table(
            "community_server_channels",
            dal.Field("community_server_id", "integer", notnull=True),
            dal.Field("platform_channel_id", "string"),
            dal.Field("platform_channel_name", "string"),
            dal.Field("channel_type", "string", default="chat"),
            dal.Field("is_active", "boolean", default=True),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "hub_channels" not in dal.tables:
        dal.define_table(
            "hub_channels",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("name", "string", notnull=True),
            dal.Field("description", "text", default=""),
            dal.Field("channel_type", "string", default="chat"),
            dal.Field("sort_order", "integer", default=0),
            dal.Field("is_active", "boolean", default=True),
            dal.Field("allow_ad_hoc_voice", "boolean", default=False),
            dal.Field("has_chat", "boolean", default=True),
            dal.Field("has_voice", "boolean", default=False),
            dal.Field("has_video", "boolean", default=False),
            dal.Field("is_temporary", "boolean", default=False),
            dal.Field("temp_duration_minutes", "integer"),
            dal.Field("is_broadcast", "boolean", default=False),
            dal.Field("community_server_channel_id", "integer"),
            dal.Field("created_by", "integer"),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "hub_forum_posts" not in dal.tables:
        dal.define_table(
            "hub_forum_posts",
            dal.Field("hub_channel_id", "integer", notnull=True),
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("title", "string", notnull=True),
            dal.Field("body", "text"),
            dal.Field("tags", "json"),
            dal.Field("author_hub_user_id", "integer"),
            dal.Field("author_platform", "string"),
            dal.Field("author_username", "string"),
            dal.Field("author_avatar_url", "text"),
            dal.Field("is_pinned", "boolean", default=False),
            dal.Field("is_locked", "boolean", default=False),
            dal.Field("reply_count", "integer", default=0),
            dal.Field("last_reply_at", "datetime"),
            dal.Field("platform_thread_id", "string"),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "hub_forum_replies" not in dal.tables:
        dal.define_table(
            "hub_forum_replies",
            dal.Field("post_id", "integer", notnull=True),
            dal.Field("author_hub_user_id", "integer"),
            dal.Field("author_platform", "string"),
            dal.Field("author_username", "string"),
            dal.Field("author_avatar_url", "text"),
            dal.Field("content", "text", notnull=True),
            dal.Field("platform_message_id", "string"),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "community_roles" not in dal.tables:
        dal.define_table(
            "community_roles",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("name", "string", notnull=True),
            dal.Field("display_name", "string"),
            dal.Field("description", "text"),
            dal.Field("is_system", "boolean", default=False),
            dal.Field("role_kind", "integer", default=0),
            dal.Field("priority", "integer", default=0),
            dal.Field("base_claims", "json"),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "hub_channel_permission_overrides" not in dal.tables:
        dal.define_table(
            "hub_channel_permission_overrides",
            dal.Field("hub_channel_id", "integer", notnull=True),
            dal.Field("community_role_id", "integer", notnull=True),
            dal.Field("grant_scopes", "json"),
            dal.Field("deny_scopes", "json"),
            dal.Field("scope", "string", default="both"),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "community_members" not in dal.tables:
        dal.define_table(
            "community_members",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("user_id", "string"),
            dal.Field("community_role_id", "integer"),
            dal.Field("claims_cache", "json"),
            dal.Field("is_active", "boolean", default=True),
            migrate=migrate,
        )

    if "inventory_items" not in dal.tables:
        dal.define_table(
            "inventory_items",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("name", "string", notnull=True),
            dal.Field("description", "text"),
            dal.Field("item_type", "string"),
            dal.Field("category", "string"),
            dal.Field("quantity", "integer", default=0),
            dal.Field("available_quantity", "integer", default=0),
            dal.Field("metadata", "json"),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            dal.Field("deleted_at", "datetime"),
            migrate=migrate,
        )

    if "inventory_checkouts" not in dal.tables:
        dal.define_table(
            "inventory_checkouts",
            dal.Field("item_id", "integer", notnull=True),
            dal.Field("user_id", "integer", notnull=True),
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("quantity", "integer", default=1),
            dal.Field("checked_out_at", "datetime"),
            dal.Field("due_at", "datetime"),
            dal.Field("returned_at", "datetime"),
            dal.Field("status", "string", default="active"),
            dal.Field("notes", "text"),
            migrate=migrate,
        )

    if "inventory_log" not in dal.tables:
        dal.define_table(
            "inventory_log",
            dal.Field("item_id", "integer"),
            dal.Field("user_id", "integer"),
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("action", "string", notnull=True),
            dal.Field("quantity_change", "integer"),
            dal.Field("details", "json"),
            dal.Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "community_raffle_sounds" not in dal.tables:
        dal.define_table(
            "community_raffle_sounds",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("event_type", "string", notnull=True),
            dal.Field("sound_url", "text"),
            dal.Field("sound_filename", "string"),
            dal.Field("sound_size_bytes", "integer"),
            dal.Field("sound_format", "string"),
            dal.Field("message_template", "text"),
            dal.Field("is_active", "boolean", default=True),
            dal.Field("created_at", "datetime"),
            dal.Field("updated_at", "datetime"),
            migrate=migrate,
        )


def community_in_tenant(dal: Any, community_id: int, ctx: TenantContext) -> bool:
    """True iff `community_id` exists and belongs to the caller's validated tenant.

    The single tenant-isolation gate every Community-module handler calls
    before touching any `community_id`-scoped table (security.md: "all
    queries/cache/service calls scoped to token's tenant") -- once this
    passes, every further query keyed on the same `community_id` is
    provably tenant-safe without re-deriving `tenant_scoped()` per table.
    """
    row = (
        dal((dal.communities.id == community_id) & (dal.communities.tenant_id == ctx.tenant_id))
        .select()
        .first()
    )
    return row is not None


def is_valid_service_key(request: Any) -> bool:
    """Check the `X-Service-Key` header against `SERVICE_API_KEY` for internal-only endpoints.

    Mirrors Node's `routes/internal.js`/`internalRelayRouter` (API-key
    auth, no tenant/JWT) via `flask_core.auth.verify_service_key`'s
    constant-time comparison. Fails closed: no `SERVICE_API_KEY`
    configured means every request is rejected, never silently allowed.
    """
    provided = request.headers.get("X-Service-Key", "")
    expected = os.getenv("SERVICE_API_KEY")
    return bool(verify_service_key(provided, expected))


def api_error(message: str, status_code: int) -> tuple[dict[str, object], int]:
    """Typed error envelope, same shape as `flask_core.api_utils.error_response`.

    `error_response` itself is untyped (flask_core has no py.typed marker,
    `follow_imports = "skip"` in pyproject.toml's mypy overrides), so
    returning it directly from a handler declared to return a specific DTO
    union trips mypy --strict's `no-any-return`. This hand-rolls the same
    `{success, error: {message, code, timestamp}}` shape with a concrete
    return type instead, so every Community-module handler can return an
    error without leaking `Any` into its signature.
    """
    return {
        "success": False,
        "error": {
            "message": message,
            "code": f"ERROR_{status_code}",
            "timestamp": datetime.utcnow().isoformat(),
        },
    }, status_code


def sql_bool(value: Any) -> bool:
    """Coerce a raw `dal.executesql` boolean column value to a real Python `bool`.

    `dal(query).select()` (the ORM query builder) decodes a boolean
    column back to `True`/`False` automatically; `executesql` -- used
    throughout this port for aggregate/window queries the ORM can't
    express -- returns the driver's raw value instead. On Postgres
    that's already a native `bool`; on SQLite, pydal stores booleans as
    `'T'`/`'F'` char columns, so the raw driver value is the *string*
    `'F'` -- truthy in plain Python (`if raw_value:` silently always
    passes). Route any executesql'd boolean column through this before
    a truthiness check.
    """
    return value in (True, 1, "1", "t", "T", "true", "True")


def get_current_user(request: Any) -> dict[str, Any]:
    """Typed accessor for `request.current_user` (set by `flask_core.api_utils.auth_required`).

    `Request`/`RequestProxy` carry no `current_user` attribute in Quart's
    own type stubs -- `auth_required` injects it at runtime. `request`
    is typed `Any` here (mypy can't check attributes on it), so the
    single `no-any-return` this function triggers is centralized once
    instead of every call site repeating an ignore.
    """
    return request.current_user  # type: ignore[no-any-return]
