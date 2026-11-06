"""pydal table bindings for the Bot module (M5).

Schema already exists -- created by `config/postgres/migrations/003,
009, 046 (shoutout), 042+055 (server manager / rcon), 067 (ai
knowledge)`. Every `define_table` call here passes `migrate=False`,
matching the established convention for tables whose DDL is owned
elsewhere (`app_bundle_tables.py`'s own docstring lists the precedent
modules) -- pydal maps onto the already-migrated table, it never owns
this DDL.

`bind_bot_tables(dal)` is idempotent-safe to call once per process
(mirrors `app.py::_bind_reference_tables`'s single-call-at-startup
shape). Core Identity/Tenancy (`hub_users`, `communities`) haven't
landed yet as of this M5 port -- M1/M2 in the migration plan, not built
this PR -- so `bind_bot_tables` also defines minimal, `migrate=False`
stand-ins for the two columns Bot's tables actually reference
(`communities.id`/`.tenant_id` for tenant-membership verification,
`hub_users.id`/`.display_name` for the rcon command-log join), guarded
so a later M1/M2 port that defines the real, fuller table first always
wins (`"communities" not in dal.tables`) -- same "table object to
resolve against, never owns the DDL" shape as `app.py`'s own
`_bind_reference_tables` for `tenants`.

Live-controller note: `adminController.js`'s shoutout functions (the
code actually wired to `/api/v1/admin/:communityId/shoutout/*` via
`routes/admin.js`) use `shoutout_creators` / `shoutout_history`
(migration 046) -- NOT the same-named-but-different
`auto_shoutout_creators` / `video_shoutout_history` tables migration
009 also defines and the orphaned, unrouted
`controllers/shoutoutController.js` reads. This binds the tables the
live routes actually use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def bind_bot_tables(dal: Any) -> None:
    """Define every table the Bot module's ported services query, `migrate=False`."""
    # -- Minimal reference-table stand-ins (M1/M2 not yet ported -- see module docstring) --
    if "communities" not in dal.tables:
        dal.define_table(
            "communities",
            dal.Field("tenant_id", "reference tenants", notnull=True),
            migrate=False,
        )
    if "hub_users" not in dal.tables:
        dal.define_table(
            "hub_users",
            dal.Field("display_name", "string", length=255),
            dal.Field("is_super_admin", "boolean", default=False),
            migrate=False,
        )

    # -- Shoutout (migration 009 shoutout_config; migration 046 shoutout_creators/history) --
    dal.define_table(
        "shoutout_config",
        dal.Field("community_id", "reference communities", notnull=True),
        dal.Field("so_enabled", "boolean", default=True),
        dal.Field("so_permission", "string", length=20, default="mod"),
        dal.Field("vso_enabled", "boolean", default=True),
        dal.Field("vso_permission", "string", length=20, default="mod"),
        dal.Field("auto_shoutout_mode", "string", length=20, default="disabled"),
        dal.Field("trigger_first_message", "boolean", default=False),
        dal.Field("trigger_raid_host", "boolean", default=True),
        dal.Field("widget_position", "string", length=20, default="bottom-right"),
        dal.Field("widget_duration_seconds", "integer", default=30),
        dal.Field("cooldown_minutes", "integer", default=60),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        dal.Field("updated_at", "datetime", default=datetime.utcnow, update=datetime.utcnow),
        migrate=False,
    )
    dal.define_table(
        "shoutout_creators",
        dal.Field("community_id", "reference communities", notnull=True),
        dal.Field("platform", "string", length=50, notnull=True),
        dal.Field("platform_username", "string", length=255, notnull=True),
        dal.Field("added_by", "reference hub_users"),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        migrate=False,
    )
    dal.define_table(
        "shoutout_history",
        dal.Field("community_id", "reference communities", notnull=True),
        dal.Field("platform", "string", length=50, notnull=True),
        dal.Field("target_username", "string", length=255, notnull=True),
        dal.Field("shoutout_type", "string", length=20, default="text"),
        dal.Field("triggered_by_username", "string", length=255),
        dal.Field("trigger_type", "string", length=30, default="manual"),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        migrate=False,
    )

    # -- Server Manager / RCON (migrations 042 base + 055 rcon/access columns) --
    dal.define_table(
        "server_status_configs",
        dal.Field("community_id", "reference communities", notnull=True),
        dal.Field("display_name", "string", length=100),
        dal.Field("game_name", "string", length=200, notnull=True),
        dal.Field("server_type", "string", length=30, default="status_only"),
        dal.Field("host", "string", length=255),
        dal.Field("game_port", "integer"),
        dal.Field("rcon_port", "integer"),
        dal.Field("credential_enc", "blob"),
        dal.Field("credential_iv", "blob"),
        dal.Field("game_type", "string", length=50, default="other"),
        dal.Field("visibility", "string", length=30, default="admin_only"),
        dal.Field("status_api_type", "string", length=20, default="custom_url"),
        dal.Field("status_url", "string", length=1000),
        dal.Field("added_by", "reference hub_users"),
        dal.Field("metadata", "json", default={}),
        dal.Field("is_active", "boolean", default=True),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        dal.Field("updated_at", "datetime", default=datetime.utcnow, update=datetime.utcnow),
        dal.Field("deleted_at", "datetime"),
        migrate=False,
    )
    dal.define_table(
        "rcon_command_log",
        dal.Field("server_config_id", "reference server_status_configs", notnull=True),
        dal.Field("user_id", "reference hub_users"),
        dal.Field("command", "text", notnull=True),
        dal.Field("response_summary", "text"),
        dal.Field("success", "boolean", default=True),
        dal.Field("executed_at", "datetime", default=datetime.utcnow),
        migrate=False,
    )
    dal.define_table(
        "server_access_policies",
        dal.Field("server_config_id", "reference server_status_configs", notnull=True),
        dal.Field("community_id", "reference communities", notnull=True),
        dal.Field("require_community_member", "boolean", default=False),
        dal.Field("auto_kick_enabled", "boolean", default=False),
        dal.Field("auto_kick_threshold", "integer", default=450),
        dal.Field("auto_ban_enabled", "boolean", default=False),
        dal.Field("auto_ban_threshold", "integer", default=350),
        dal.Field("auto_ban_duration_hours", "integer"),
        dal.Field("min_reputation_to_join", "integer"),
        dal.Field("sync_interval_minutes", "integer", default=5),
        dal.Field("notify_on_action", "boolean", default=True),
        dal.Field("exempt_roles", "list:string", default=[]),
        dal.Field("sync_to_community", "boolean", default=False),
        dal.Field("last_enforced_at", "datetime"),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        dal.Field("updated_at", "datetime", default=datetime.utcnow, update=datetime.utcnow),
        migrate=False,
    )
    dal.define_table(
        "server_access_log",
        dal.Field("server_config_id", "reference server_status_configs", notnull=True),
        dal.Field("target_player", "string", length=255, notnull=True),
        dal.Field("action", "string", length=30, notnull=True),
        dal.Field("reason", "text"),
        dal.Field("reputation_score", "integer"),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        migrate=False,
    )

    # -- AI Knowledge (migration 067) --
    dal.define_table(
        "ai_knowledge_sources",
        dal.Field("community_id", "reference communities"),
        dal.Field("vendor_id", "reference hub_users"),
        dal.Field("module_id", "integer"),
        dal.Field("source_name", "string", length=255, notnull=True),
        dal.Field("source_type", "string", length=30, notnull=True),
        dal.Field("source_url", "text"),
        dal.Field("branch", "string", length=100, default="main"),
        dal.Field("docs_path", "string", length=500, default="/"),
        dal.Field("refresh_interval", "string", length=20, default="weekly"),
        dal.Field("encrypted_token", "text"),
        dal.Field("is_active", "boolean", default=True),
        dal.Field("last_indexed_at", "datetime"),
        dal.Field("indexed_page_count", "integer", default=0),
        dal.Field("index_errors", "text"),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        dal.Field("updated_at", "datetime", default=datetime.utcnow, update=datetime.utcnow),
        migrate=False,
    )
    dal.define_table(
        "ai_ticket_suggestions",
        dal.Field("ticket_id", "integer", notnull=True),
        dal.Field("suggestion_text", "text", notnull=True),
        dal.Field("confidence_score", "double", notnull=True),
        dal.Field("cited_chunks", "list:integer", default=[]),
        dal.Field("feedback", "string", length=20),
        dal.Field("is_auto_posted", "boolean", default=False),
        dal.Field("created_at", "datetime", default=datetime.utcnow),
        migrate=False,
    )
    # `ai_knowledge_chunks.embedding` (pgvector `vector(384)`) is written
    # and queried via raw SQL (`bot_ai_knowledge.py`'s `_upsert_chunk` /
    # `_similarity_search`) -- pydal has no native pgvector field type, and
    # the `<=>` cosine-distance operator + `::vector` cast used by the
    # ported Node query aren't expressible through the query builder. No
    # `define_table` for this one; raw SQL runs against `dal._adapter`.


def community_belongs_to_tenant(dal: Any, community_id: int, tenant_id: int) -> bool:
    """True iff `community_id` resolves to a `communities` row owned by `tenant_id`.

    security.md Tenant Isolation: "Trust tenant ID from request body/param"
    is a NEVER -- every Bot route takes `community_id` from the URL path
    (matching the frozen `/api/v1` contract, which predates tenant
    scoping), so every handler in `blueprints/v1/bot.py` calls this
    immediately after `tenant_middleware` resolves `TenantContext` and
    before touching any Bot-module table, rejecting with 403 on a
    mismatch. One-hop equivalent of `flask_core.tenancy.tenant_scoped`'s
    own `community_id -> communities.tenant_id` join, applied once at the
    path-param boundary rather than re-derived inside every query.
    """
    row = (
        dal((dal.communities.id == community_id) & (dal.communities.tenant_id == tenant_id))
        .select()
        .first()
    )
    return row is not None
