"""Economy ledger kinds for the compound host-cap ops (P4 Task 1).

Extends the `economy_ledger.kind` CHECK to allow `credit`, `debit`,
`admin_adjust`, `pot_payout` and `exchange` (the new `bundle_host_economy`
ops). Idempotent; reaches existing databases. The DDL lives in
`scripts/db/bundle_economy_ledger_kinds.sql` so the shipped schema and the
Rust-tested schema cannot drift. No new privilege.

Revision ID: 0069_economy_ledger_kinds
Revises: 0068_app_catalog_kind
Create Date: 2026-10-30
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0069_economy_ledger_kinds"
down_revision = "0068_app_catalog_kind"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_economy_ledger_kinds.sql"
)


def upgrade() -> None:
    """Re-create the kind CHECK with the compound-op kinds."""
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Restore the original three-kind CHECK (fails if new-kind rows exist)."""
    op.execute("ALTER TABLE economy_ledger DROP CONSTRAINT IF EXISTS economy_ledger_kind_check")
    op.execute(
        "ALTER TABLE economy_ledger ADD CONSTRAINT economy_ledger_kind_check "
        "CHECK (kind IN ('wager', 'transfer_out', 'transfer_in'))"
    )
