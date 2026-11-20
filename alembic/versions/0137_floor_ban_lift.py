"""Explicit LIFTED state for automated floor bans (status 8, `lifted_at`, `lifted_by`).

A floor ban an admin reverses used to keep denying forever because the outbox had no terminal
"lifted" status. DDL, the `reputation_floor_ban_lift()` SECURITY DEFINER function and the
lifted-aware erasure function live in `scripts/db/bundle_reputation_floor_ban_lift.sql`.

Revision ID: 0137_floor_ban_lift
Revises: 0136_app_id_owners
Create Date: 2026-11-20
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0137_floor_ban_lift"
down_revision = "0136_app_id_owners"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_reputation_floor_ban_lift.sql"
)


def upgrade() -> None:
    """Add the lift columns, status 8 and the lift function (idempotent)."""
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Return lifted rows to done, then drop the lift objects and restore the 1..7 check."""
    op.execute("UPDATE bundle_reputation_floor_bans SET status = 2 WHERE status = 8")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_bundle_reputation_floor_bans_clear_lift "
        "ON bundle_reputation_floor_bans"
    )
    op.execute("DROP FUNCTION IF EXISTS reputation_floor_ban_clear_lift()")
    op.execute(
        "DROP FUNCTION IF EXISTS reputation_floor_ban_lift(bigint, uuid[], uuid)"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_floor_bans "
        "DROP CONSTRAINT IF EXISTS ck_bundle_reputation_floor_bans_status"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_floor_bans ADD CONSTRAINT "
        "bundle_reputation_floor_bans_status_check CHECK (status IN (1, 2, 3, 4, 5, 6, 7))"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_floor_bans DROP COLUMN IF EXISTS lifted_by"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_floor_bans DROP COLUMN IF EXISTS lifted_at"
    )
