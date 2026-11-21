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

A table absent from the database (the minimal CI bootstrap never replays the legacy SQL baseline) is
skipped with a WARNING -- same precedent as 0091/0093; a full chain always has them all and the
real-Postgres test asserts it.

Revision ID: 0137_entity_public_ids
Revises: 0136_app_id_owners
Create Date: 2026-11-21
"""

from __future__ import annotations

import logging

from alembic import op

revision = "0137_entity_public_ids"
down_revision = "0136_app_id_owners"
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


def _exists(table: str) -> bool:
    """True when `table` exists in the connected database (offline mode: assume it does)."""
    bind = op.get_bind()
    if bind is None:
        return True
    return bool(bind.exec_driver_sql("SELECT to_regclass(%s)", (table,)).scalar())


def upgrade() -> None:
    """Add, backfill and uniquely constrain `public_id` on every listed table (idempotent)."""
    for table in ENTITY_TABLES:
        if not _exists(table):
            _LOG.warning("entity_public_ids: table %s absent; skipped", table)
            continue
        op.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
        )
        op.execute(f"UPDATE {table} SET public_id = gen_random_uuid() WHERE public_id IS NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN public_id SET DEFAULT gen_random_uuid()")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN public_id SET NOT NULL")
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_public_id ON {table} (public_id)")


def downgrade() -> None:
    """Drop the unique index and the column from every listed table."""
    for table in ENTITY_TABLES:
        if not _exists(table):
            continue
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_public_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS public_id")
