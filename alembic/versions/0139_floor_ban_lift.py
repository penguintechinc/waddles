"""Explicit LIFTED state for automated floor bans (status 8, `lifted_at`, `lifted_by`).

A floor ban an admin reverses used to keep denying forever because the outbox had no terminal
"lifted" status. DDL, the `reputation_floor_ban_lift()` SECURITY DEFINER function and the
lifted-aware erasure function live in `scripts/db/bundle_reputation_floor_ban_lift.sql`.

Revision ID: 0139_floor_ban_lift
Revises: 0138_entity_public_ids
Create Date: 2026-11-20
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0139_floor_ban_lift"
down_revision = "0138_entity_public_ids"
branch_labels = None
depends_on = None

_DB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "db"
_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban_lift.sql"
_BASE_SQL_PATH = _DB_DIR / "bundle_reputation_floor_ban.sql"


def upgrade() -> None:
    """Add the lift columns, status 8 and the lift function (idempotent)."""
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Return lifted rows to done, drop the lift objects, restore the 1..7 check and 0114 functions."""
    op.execute("UPDATE bundle_reputation_floor_bans SET status = 2 WHERE status = 8")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_bundle_reputation_floor_bans_clear_lift "
        "ON bundle_reputation_floor_bans"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_bundle_reputation_scores_floor_recovered "
        "ON bundle_reputation_scores"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_bundle_reputation_tenant_scores_floor_recovered "
        "ON bundle_reputation_tenant_scores"
    )
    op.execute("DROP FUNCTION IF EXISTS reputation_floor_ban_mark_recovered()")
    op.execute(
        "DROP INDEX IF EXISTS idx_bundle_reputation_floor_bans_lifted_unrecovered"
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
    for column in ("recovered_at", "lifted_by", "lifted_at"):
        op.execute(
            f"ALTER TABLE bundle_reputation_floor_bans DROP COLUMN IF EXISTS {column}"
        )
    # Restore the 0114 enqueue / erase functions (they no longer match the dropped columns).
    op.get_bind().exec_driver_sql(_BASE_SQL_PATH.read_text(encoding="utf-8"))
