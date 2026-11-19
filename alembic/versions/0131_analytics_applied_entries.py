"""Analytics writer idempotency ledger: `analytics_ops.applied_entries`.

`svc_analytics` consumes the `waddles:analytics` Valkey stream at-least-once (ack after the
database commit). To make a replay after a crash between commit and ack a no-op, each batch
inserts the stream entry ids it applies into this table IN THE SAME TRANSACTION as the layer A
upserts and applies only entries whose insert succeeded (`ON CONFLICT DO NOTHING`).

The table holds stream entry ids only (`<ms>-<seq>`): no user, community or content data. It
lives in its own schema (`analytics_ops`) so the layer A schema stays exactly the data model of
`0129_analytics_foundation`. Rows older than 7 days are purged by the service (a redelivery
horizon far beyond the consumer's claim window).

Role grants: `analytics_writer` only (SELECT/INSERT/DELETE on the ledger).

Also grants `analytics_writer` the narrowest read the writer needs to tell Premium from Free
communities: column-level SELECT on `public.community_tier_allocations (community_id, tier_code,
released_at)` -- nothing else on that table (no tenant_id, allocated_by, ...), no other table.
Guarded by role existence and idempotent; downgrade revokes exactly that. The service connects
as the LOGIN role `waddles_svc_analytics` (a member of `analytics_writer`), so it inherits both.

All DDL is idempotent.

Revision ID: 0131_analytics_applied_entries
Revises: 0130_analytics_exclusions_grant
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "0131_analytics_applied_entries"
down_revision = "0130_analytics_exclusions_grant"
branch_labels = None
depends_on = None

_GRANT_TIER = (
    "DO $$ BEGIN\n"
    "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_writer') THEN\n"
    "    GRANT SELECT (community_id, tier_code, released_at)\n"
    "      ON public.community_tier_allocations TO analytics_writer;\n"
    "  END IF;\n"
    "END $$"
)

_UPGRADE = (
    "SET LOCAL lock_timeout = '10s'",
    "CREATE SCHEMA IF NOT EXISTS analytics_ops",
    """
CREATE TABLE IF NOT EXISTS analytics_ops.applied_entries (
    entry_ms   BIGINT NOT NULL CHECK (entry_ms >= 0),
    entry_seq  BIGINT NOT NULL CHECK (entry_seq >= 0),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entry_ms, entry_seq)
)
""",
    "CREATE INDEX IF NOT EXISTS ix_ae_applied_at ON analytics_ops.applied_entries (applied_at)",
    "REVOKE ALL ON SCHEMA analytics_ops FROM PUBLIC",
    "REVOKE ALL ON ALL TABLES IN SCHEMA analytics_ops FROM PUBLIC",
    "GRANT USAGE ON SCHEMA analytics_ops TO analytics_writer",
    "GRANT SELECT, INSERT, DELETE ON analytics_ops.applied_entries TO analytics_writer",
    _GRANT_TIER,
)
_REVOKE_TIER = (
    "DO $$ BEGIN\n"
    "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_writer') THEN\n"
    "    REVOKE SELECT (community_id, tier_code, released_at)\n"
    "      ON public.community_tier_allocations FROM analytics_writer;\n"
    "  END IF;\n"
    "END $$"
)


def upgrade() -> None:
    """Create the idempotency ledger, grant it and the tier-column read to the writer role."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop the ledger (it is replay-dedupe state only; dropping it loses no analytics data)."""
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_REVOKE_TIER)
    op.execute("DROP TABLE IF EXISTS analytics_ops.applied_entries")
    op.execute("DROP SCHEMA IF EXISTS analytics_ops")
