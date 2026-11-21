"""`public_id UUID` on every entity table still addressed by its numeric id in an API URL.

Entity-identity rule: the numeric BIGINT/SERIAL PK is internal (joins/FKs); the `public_id` UUID is
the only identifier that crosses an API boundary (enumerable ids are an IDOR primitive). The v3.0.x
route-conversion PRs replace every remaining `<int:...>` path param with the entity's `public_id`;
this migration is the schema groundwork they all share, so none of them carries a migration.

Same add/backfill/constrain idiom as `0122_ingest_sources_public_id`:
`ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()` (the volatile default is
evaluated once per existing row, atomically -- a still-running old pod can never insert a NULL), a
repair `UPDATE`/`SET DEFAULT`/`SET NOT NULL` for a half-applied earlier run, then a UNIQUE index.

Already covered elsewhere (no change): `communities`, `tenants`, `app_catalog` (0084),
`platform_connections` (0063), `hub_users.uuid`, `ingest_sources` (0122).
Deliberately excluded: `community_polls` / `community_forms` (their routes are deleted) and
`tier_code` (an int enum code, not an entity id).

A table absent from the database is skipped ONLY when it is on `MINIMAL_BOOTSTRAP_ABSENT` -- the
tables known to be missing from the minimal CI bootstrap (which never replays the legacy SQL baseline
nor migrations before 0020) -- and that skip is logged at INFO. Any other absent table (a typo'd
name, a renamed table) RAISES, so a full chain can never silently end up without a `public_id`.

Revision ID: 0138_entity_public_ids
Revises: 0137_discord_guild_tenants
Create Date: 2026-11-21
"""

from __future__ import annotations

import logging

from alembic import op

revision = "0138_entity_public_ids"
down_revision = "0137_discord_guild_tenants"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")

#: table -> route param it backs (documentation for the follow-up route PRs).
ENTITY_TABLES: dict[str, str] = {
    # A: bot / rcon / ai / pairing / join requests / tokens
    "server_status_configs": "server_id",
    "ai_knowledge_sources": "source_id",
    "ai_ticket_suggestions": "suggestion_id",
    "shoutout_creators": "creator_id",
    "guild_tenant_pairings": "pairing_id",
    "community_role_sync_bindings": "binding_id",
    "discord_role_mirror_mappings": "mapping_id",
    "community_join_requests": "request_id",
    "community_access_tokens": "token_id",
    # B: community content
    "announcements": "announcement_id",
    "inventory_items": "item_id",
    "hub_channels": "channel_id",
    "hub_forum_posts": "post_id",
    "hub_forum_replies": "reply_id",
    "community_roles": "role_id",
    "support_ticket_categories": "category_id",
    "support_tickets": "ticket_id",
    "community_radio_stations": "station_id",
    # C: marketplace / billing / modules
    "vendor_discount_codes": "code_id",
    "marketplace_subscriptions": "subscription_id",
    "hub_modules": "module_id",
    "marketplace_modules": "module_id",
    "marketplace_submissions": "submission_id",
    "vendor_submissions": "submission_id",
    # D: identity leftovers (`tenants` already has public_id; `tenant_license_pools` is addressed
    # by (tenant UUID, tier_code), never by its own row id, so it needs none)
    "hub_sessions": "session_id",
    "user_passkeys": "credential_id",
}


#: Tables the minimal CI bootstrap (`alembic/tests/pg_docker.py`: stub FK tables + `alembic stamp
#: 0019`) does not have -- their DDL lives in legacy SQL migrations or in migrations before 0020.
#: Measured by migrating that bootstrap to head; every other listed table is created by 0020+.
MINIMAL_BOOTSTRAP_ABSENT: frozenset[str] = frozenset(
    {
        "ai_knowledge_sources",
        "ai_ticket_suggestions",
        "announcements",
        "community_access_tokens",
        "community_join_requests",
        "community_radio_stations",
        "community_roles",
        "hub_channels",
        "hub_forum_posts",
        "hub_forum_replies",
        "hub_modules",
        "hub_sessions",
        "inventory_items",
        "marketplace_modules",
        "marketplace_submissions",
        "marketplace_subscriptions",
        "server_status_configs",
        "shoutout_creators",
        "support_ticket_categories",
        "support_tickets",
        "user_passkeys",
        "vendor_discount_codes",
        "vendor_submissions",
    }
)


def _present(table: str) -> bool:
    """True to migrate `table`; False to skip a known-absent one; raises for any other absence."""
    bind = op.get_bind()
    if bind is None:  # offline SQL generation: assume the full chain
        return True
    if bind.exec_driver_sql("SELECT to_regclass(%s)", (table,)).scalar():
        return True
    if table in MINIMAL_BOOTSTRAP_ABSENT:
        _LOG.info("entity_public_ids: %s absent (minimal CI bootstrap); skipped", table)
        return False
    raise RuntimeError(
        f"entity_public_ids: table {table!r} does not exist and is not a known minimal-bootstrap "
        "absentee; fix its name in ENTITY_TABLES (or add it to MINIMAL_BOOTSTRAP_ABSENT)"
    )


def upgrade() -> None:
    """Add, backfill and uniquely constrain `public_id` on every listed table (idempotent)."""
    for table in ENTITY_TABLES:
        if not _present(table):
            continue
        op.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
        )
        op.execute(
            f"UPDATE {table} SET public_id = gen_random_uuid() WHERE public_id IS NULL"
        )
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN public_id SET DEFAULT gen_random_uuid()"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN public_id SET NOT NULL")
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_public_id ON {table} (public_id)"
        )


def downgrade() -> None:
    """Drop the unique index and the column from every listed table."""
    for table in ENTITY_TABLES:
        if not _present(table):
            continue
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_public_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS public_id")
