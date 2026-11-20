"""Backfill `shoutout_history` columns that hub_api reads but fresh-replay never created.

Legacy `config/postgres/migrations/003_add_shoutout_tables.sql` created `shoutout_history` with
`(target_user_id, message NOT NULL, ...)`; `046_add_remaining_admin_tables.sql` later declared the
shape hub-api actually uses (`shoutout_type`, `triggered_by_username`, `trigger_type`) but its
`CREATE TABLE IF NOT EXISTS` is a no-op wherever 003 ran first (every real DB and every fresh
replay). `GET /community/{id}/shoutout/history` therefore died with `UndefinedColumn
shoutout_type`, poisoning the pooled transaction.

Adds the three columns with 046's types/defaults (all idempotent) and drops NOT NULL on the
legacy `message` column, which 046's intended shape does not have and which nothing in the repo
writes. Downgrade drops the three added columns; the `message` relaxation is deliberately not
reverted (re-adding NOT NULL could fail on rows written while it was relaxed).

Revision ID: 0134_shoutout_history_drift
Revises: 0133_precompile_stale_reclaims
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision = "0134_shoutout_history_drift"
down_revision = "0133_precompile_stale_reclaims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the 046-shape columns to `shoutout_history` (idempotent).

    `IF EXISTS`: the stamped-minimal-schema test harness (`migrated_postgres`) has no legacy
    tables; on every real/fresh-replayed DB 0001 has already created `shoutout_history`.
    """
    op.execute(
        "ALTER TABLE IF EXISTS shoutout_history "
        "ADD COLUMN IF NOT EXISTS shoutout_type VARCHAR(20) DEFAULT 'text'"
    )
    op.execute(
        "ALTER TABLE IF EXISTS shoutout_history ADD COLUMN IF NOT EXISTS triggered_by_username VARCHAR(255)"
    )
    op.execute(
        "ALTER TABLE IF EXISTS shoutout_history "
        "ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(30) DEFAULT 'manual'"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'shoutout_history' AND column_name = 'message'
            ) THEN
                ALTER TABLE shoutout_history ALTER COLUMN message DROP NOT NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the added columns (the `message` NOT NULL relaxation is intentionally kept)."""
    op.execute(
        "ALTER TABLE IF EXISTS shoutout_history DROP COLUMN IF EXISTS trigger_type"
    )
    op.execute(
        "ALTER TABLE IF EXISTS shoutout_history DROP COLUMN IF EXISTS triggered_by_username"
    )
    op.execute(
        "ALTER TABLE IF EXISTS shoutout_history DROP COLUMN IF EXISTS shoutout_type"
    )
