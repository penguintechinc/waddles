"""Reputation automated hard ban: floor re-verification, audit/idempotency/retry store.

Adds `reputation_verify_floor_breach()` (the platform ban tool re-verifies a floor breach in SQL
before acting), the `bundle_reputation_floor_bans` audit + idempotency + durable-retry table with
its SECURITY DEFINER enqueue/claim/recheck/complete/reap outbox functions, `reputation_tenant_communities()` (the tenant-ban
fan-out set) and the per-community `floor_ban_hourly_quota` policy column. DDL lives in
`scripts/db/bundle_reputation_floor_ban.sql` (shared with the Rust crate's integration test).

Revision ID: 0102_reputation_floor_ban
Revises: 0101_bundle_precompile_requests

Chain note: chained after 0101_bundle_precompile_requests (the release head when written). If another migration lands first,
re-chain `down_revision` onto it so the graph keeps a single head.
Create Date: 2026-11-11
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0102_reputation_floor_ban"
down_revision = "0101_bundle_precompile_requests"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_reputation_floor_ban.sql"
)


def upgrade() -> None:
    """Apply the floor-ban DDL (multi-statement script with `$$` blocks)."""
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Drop the floor-ban functions, table and policy column."""
    for sig in (
        "reputation_verify_floor_breach(bigint, bigint, uuid, smallint)",
        "reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text)",
        "reputation_floor_ban_reap(int)",
        "reputation_floor_ban_claim(int, int)",
        "reputation_floor_ban_backlog()",
        "reputation_floor_ban_halted()",
        "reputation_floor_ban_set_halted(boolean, text)",
        "reputation_floor_ban_recheck(bigint, uuid)",
        "reputation_floor_ban_complete(bigint, uuid, smallint, text, text)",
        "reputation_tenant_communities(bigint)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_floor_bans")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_floor_ban_control")
    op.execute(
        "ALTER TABLE bundle_reputation_policy DROP COLUMN IF EXISTS floor_ban_hourly_quota"
    )
