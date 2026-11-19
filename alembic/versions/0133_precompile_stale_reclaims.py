"""Stale-lease reclaim counter for the bundle precompile queue (poison-pill guard).

`bundle_precompile_requests.stale_reclaims` counts how many times a row's lease expired with no
ack (runner crash / OOM / timeout), separately from `attempts` (genuine `failed` acks only).
`claim_pending()` dead-letters a row once this exceeds `MAX_STALE_RECLAIMS`, so an item that
deterministically kills its runner cannot be re-offered forever. Revive paths reset it to 0.

Additive and defaulted (`NOT NULL DEFAULT 0`), so existing rows and older hub-api pods keep
working. Downgrade drops the column.

Revision ID: 0133_precompile_stale_reclaims
Revises: 0132_community_public_id_read
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision = "0133_precompile_stale_reclaims"
down_revision = "0132_community_public_id_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the stale-reclaim counter (idempotent)."""
    op.execute(
        "ALTER TABLE bundle_precompile_requests "
        "ADD COLUMN IF NOT EXISTS stale_reclaims INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE bundle_precompile_requests "
        "DROP CONSTRAINT IF EXISTS ck_bundle_precompile_requests_stale_reclaims"
    )
    op.execute(
        "ALTER TABLE bundle_precompile_requests "
        "ADD CONSTRAINT ck_bundle_precompile_requests_stale_reclaims CHECK (stale_reclaims >= 0)"
    )


def downgrade() -> None:
    """Drop the counter (and its CHECK) -- restores the 0108 shape."""
    op.execute(
        "ALTER TABLE bundle_precompile_requests "
        "DROP CONSTRAINT IF EXISTS ck_bundle_precompile_requests_stale_reclaims"
    )
    op.execute("ALTER TABLE bundle_precompile_requests DROP COLUMN IF EXISTS stale_reclaims")
