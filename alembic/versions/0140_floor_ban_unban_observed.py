"""Floor-ban platform unban: time-bounded lift (`p_observed_ms`) and guild-wide lift.

A platform unban lifts only bans that existed when it was observed (a stale event never lifts a
newer ban), and a Discord-native unban lifts every community of ONE tenant linked to the guild. DDL
and functions live in `scripts/db/bundle_reputation_floor_ban_platform_unban_guild.sql`.

Revision ID: 0140_floor_ban_unban_observed
Revises: 0139_floor_ban_rearm_policy
Create Date: 2026-11-21
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0140_floor_ban_unban_observed"
down_revision = "0139_floor_ban_rearm_policy"
branch_labels = None
depends_on = None

_DB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "db"
_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban_platform_unban_guild.sql"
_SYNC_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban_platform_sync.sql"


def upgrade() -> None:
    """Replace the 5-arg lift with the observed-time one and add the guild-wide lift."""
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Drop the new functions and restore the 0138 5-arg lift (re-applies the idempotent 0138 SQL)."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_bundle_reputation_floor_bans_armed_at "
        "ON bundle_reputation_floor_bans"
    )
    op.execute("DROP FUNCTION IF EXISTS reputation_floor_ban_set_armed_at()")
    op.execute("ALTER TABLE bundle_reputation_floor_bans DROP COLUMN IF EXISTS armed_at")
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "reputation_floor_ban_lift_from_platform_guild(bigint, text, text, uuid, text, bigint)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid, text, bigint)"
    )
    op.get_bind().exec_driver_sql(_SYNC_SQL_PATH.read_text(encoding="utf-8"))
