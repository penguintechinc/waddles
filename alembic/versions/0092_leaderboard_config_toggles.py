"""Add leaderboard toggle columns missing from live community_leaderboard_config.

The hub-api DAL binding (services/community_common.py) selects watch_time_enabled,
messages_enabled and public_leaderboard, but migration 004 never created them, so
GET /community/<id>/leaderboard/* 500ed with UndefinedColumn on live alpha.
"""

from alembic import op

revision = "0092_leaderboard_cfg_toggles"
down_revision = "0091_marketplace_vendor_view"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the three boolean toggles idempotently, defaulting to enabled."""
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
