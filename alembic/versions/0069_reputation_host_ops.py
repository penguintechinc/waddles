"""Reputation host-cap ops: score clamp CHECK + ledger kind/idempotency columns.

Bundle-MCP-tools P5 Task 1 adds `override`, `adjust_many`, `get_many` and
`leaderboard` to the `reputation` host capability. They need (a) a CHECK
clamping `bundle_reputation_scores.balance` (recon found none), and (b) ledger
columns separating an admin override from a capped adjust and carrying the
host-derived idempotency key. The DDL lives in
`scripts/db/bundle_reputation_host_ops.sql` (shared with the Rust crate's
integration test so the shipped and tested schema cannot drift).

Revision ID: 0069_reputation_host_ops
Revises: 0068_app_catalog_kind
Create Date: 2026-10-30
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0069_reputation_host_ops"
down_revision = "0068_app_catalog_kind"
branch_labels = None
depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_reputation_host_ops.sql"


def upgrade() -> None:
    """Apply the (idempotent) host-ops DDL."""
    # exec_driver_sql: multi-statement script with `$$` blocks; bypass text() bind parsing.
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Drop the clamp CHECK, idempotency index and the added ledger columns."""
    op.execute("DROP INDEX IF EXISTS uq_bundle_reputation_adjustments_idem")
    op.execute(
        "ALTER TABLE bundle_reputation_adjustments "
        "DROP CONSTRAINT IF EXISTS ck_bundle_reputation_adjustments_kind"
    )
    for col in ("idempotency_key", "balance_after", "requested_value", "kind"):
        op.execute(f"ALTER TABLE bundle_reputation_adjustments DROP COLUMN IF EXISTS {col}")
    op.execute(
        "ALTER TABLE bundle_reputation_scores "
        "DROP CONSTRAINT IF EXISTS ck_bundle_reputation_scores_balance_range"
    )
