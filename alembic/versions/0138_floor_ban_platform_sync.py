"""Floor-ban platform sync: unban outbox rows (`action`) and platform-originated lifts.

A Waddles lift of a delivered ban enqueues one platform UNBAN through the same outbox the ban
used; a platform-side unban lifts the matching ban without echoing one back. DDL and functions
live in `scripts/db/bundle_reputation_floor_ban_platform_sync.sql`.

Revision ID: 0138_floor_ban_platform_sync
Revises: 0137_floor_ban_lift
Create Date: 2026-11-21
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0138_floor_ban_platform_sync"
down_revision = "0137_floor_ban_lift"
branch_labels = None
depends_on = None

_DB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "db"
_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban_platform_sync.sql"
_LIFT_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban_lift.sql"
_BASE_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban.sql"


def upgrade() -> None:
    """Add `action` / `source_ban_id` / `lift_origin` and the sync functions (idempotent)."""
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Drop pending unban rows and the sync objects, then restore the 0114 + 0137 functions."""
    op.execute("DELETE FROM bundle_reputation_floor_bans WHERE action = 2")
    op.execute(
        "DROP FUNCTION IF EXISTS reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid)"
    )
    op.execute("DROP FUNCTION IF EXISTS reputation_floor_ban_claim(int, int)")
    op.execute(
        "DROP FUNCTION IF EXISTS reputation_floor_current_score(bigint, bigint, uuid, smallint)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS reputation_floor_lift_rearm_drop(bigint, bigint, smallint)"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_policy DROP COLUMN IF EXISTS lift_rearm_drop"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_tenant_policy DROP COLUMN IF EXISTS lift_rearm_drop"
    )
    for column in ("score_at_lift", "lift_origin", "source_ban_id", "action"):
        op.execute(
            f"ALTER TABLE bundle_reputation_floor_bans DROP COLUMN IF EXISTS {column}"
        )
    # The 0114 SQL recreates claim (original return type) and the base functions; the 0137 SQL
    # then re-applies the lift-aware lift / enqueue / erase definitions.
    bind = op.get_bind()
    bind.exec_driver_sql(_BASE_SQL_PATH.read_text(encoding="utf-8"))
    bind.exec_driver_sql(_LIFT_SQL_PATH.read_text(encoding="utf-8"))
