"""Economy ledger kinds for the compound host-cap ops (P4 Task 1).

Extends the `economy_ledger.kind` CHECK to allow `credit`, `debit`,
`cond_debit`, `admin_adjust`, `pot_payout` and `exchange`, and adds the
nullable replay-binding columns `rate_num`/`rate_den`/`min_balance` (the new `bundle_host_economy`
ops). Idempotent; reaches existing databases. The DDL lives in
`scripts/db/bundle_economy_ledger_kinds.sql` so the shipped schema and the
Rust-tested schema cannot drift. No new privilege.

Revision ID: 0071_economy_ledger_kinds
Revises: 0070_identity_view_role
Create Date: 2026-10-30
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0071_economy_ledger_kinds"
down_revision = "0070_identity_view_role"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_economy_ledger_kinds.sql"
)


_PHASE_MARKER = "-- @@PHASE: VALIDATE@@"


def _phases() -> tuple[str, str]:
    """Split the DDL into the catalog-only CHECK swap and the ledger-scanning VALIDATE."""
    swap, marker, validate = _SQL_PATH.read_text(encoding="utf-8").partition(
        _PHASE_MARKER
    )
    if not marker or not validate.strip():
        raise RuntimeError(f"{_SQL_PATH.name} is missing its {_PHASE_MARKER} phase")
    return swap, validate


def upgrade() -> None:
    """Swap the kind CHECK in as NOT VALID, then VALIDATE it in its own transaction.

    The swap is catalog-only (momentary ACCESS EXCLUSIVE); the ledger scan runs in a
    separate transaction under SHARE UPDATE EXCLUSIVE, so writers are never blocked
    for the duration of a full-table scan. env.py owns the outer connection
    transaction, so (as `autocommit_block` cannot be used there) the swap is
    committed explicitly before the VALIDATE starts. Both phases are idempotent, so
    a crash between them is repaired by re-running this migration.
    """
    swap, validate = _phases()
    bind = op.get_bind()
    bind.exec_driver_sql(swap)
    bind.commit()  # release the swap's ACCESS EXCLUSIVE lock before the scan
    bind.exec_driver_sql(validate)


def downgrade() -> None:
    """Restore the original three-kind CHECK (fails if new-kind rows exist)."""
    op.execute(
        "ALTER TABLE economy_ledger DROP CONSTRAINT IF EXISTS economy_ledger_kind_check"
    )
    op.execute("ALTER TABLE economy_ledger DROP COLUMN IF EXISTS min_balance")
    op.execute("ALTER TABLE economy_ledger DROP COLUMN IF EXISTS rate_den")
    op.execute("ALTER TABLE economy_ledger DROP COLUMN IF EXISTS rate_num")
    op.execute(
        "ALTER TABLE economy_ledger ADD CONSTRAINT economy_ledger_kind_check "
        "CHECK (kind IN ('wager', 'transfer_out', 'transfer_in'))"
    )
