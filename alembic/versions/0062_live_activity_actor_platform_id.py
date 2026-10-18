"""Add `live_activity_events.actor_platform_user_id` so the feed is erasure-matchable.

`live_activity_events.actor` is a raw platform handle (free text) with no stable id, so GDPR
Art.17 erasure could not locate a subject's rows (it was a pinned REVIEW gap). The platform-native
user id of the actor is now stored alongside it; the erasure registry deletes rows by the
`(platform, actor_platform_user_id)` pair. Rows written before this migration keep a NULL id and
remain unmatchable (documented residual; closed by the retention window, docs/compliance).

Idempotent (`IF NOT EXISTS`) and a no-op on the minimal CI bootstrap where the legacy table is
absent. Downgrade keeps the column (dropping it would silently discard erasure keys).

Revision ID: 0062_live_activity_actor_id
Revises: 0061_port_legacy_sql_103_codec
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op

revision = "0062_live_activity_actor_id"
down_revision = "0061_port_legacy_sql_103_codec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable erasure key + partial index (no-op if the table does not exist)."""
    op.execute(
        "ALTER TABLE IF EXISTS live_activity_events "
        "ADD COLUMN IF NOT EXISTS actor_platform_user_id VARCHAR(255)"
    )
    op.execute(
        "DO $$ BEGIN IF to_regclass('live_activity_events') IS NOT NULL THEN "
        "CREATE INDEX IF NOT EXISTS idx_live_activity_events_actor "
        "ON live_activity_events (platform, actor_platform_user_id) "
        "WHERE actor_platform_user_id IS NOT NULL; END IF; END $$"
    )


def downgrade() -> None:
    """Intentional no-op: the column is retained so erasure keys survive a rollback."""
