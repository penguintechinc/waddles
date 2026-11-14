"""Add leaderboard toggle columns missing from live community_leaderboard_config.

The hub-api DAL binding (services/community_common.py) selects watch_time_enabled,
messages_enabled and public_leaderboard, but migration 004 never created them, so
GET /community/<id>/leaderboard/* 500ed with UndefinedColumn on live alpha.

The table itself is owned by legacy SQL 004_add_missing_tables.sql. A DB that is
bootstrapped without the legacy SQL replay (the CI minimal-FK bootstrap stamped at
0019) never has it, so upgrade() first creates it IF NOT EXISTS using the exact 004
DDL -- a no-op on every DB that already has the table (alpha, full baseline replay).
"""

from alembic import op

revision = "0092_leaderboard_cfg_toggles"
down_revision = "0091_marketplace_vendor_view"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Ensure the legacy table exists, then add the three toggles idempotently."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS community_leaderboard_config (
            id SERIAL PRIMARY KEY,
            community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE UNIQUE,
            enabled_platforms JSONB DEFAULT '[]',
            min_watch_time_minutes INTEGER DEFAULT 60,
            min_message_count INTEGER DEFAULT 10,
            display_limit INTEGER DEFAULT 10,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_community_leaderboard_config_community "
        "ON community_leaderboard_config(community_id)"
    )
    for col in ("watch_time_enabled", "messages_enabled", "public_leaderboard"):
        op.execute(
            f"ALTER TABLE community_leaderboard_config "
            f"ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT TRUE"
        )


def downgrade() -> None:
    """Drop the three toggle columns."""
    for col in ("watch_time_enabled", "messages_enabled", "public_leaderboard"):
        op.execute(
            f"ALTER TABLE community_leaderboard_config DROP COLUMN IF EXISTS {col}"
        )
