"""Economy ELEVATED app-caller primitives: `transfer_from` ledger kinds + audit table.

Adds the `transfer_from` / `transfer_from_in` ledger kinds, the
`economy_elevated_audit` table (one row per elevated call by an app bundle via
the economy service: caller app, approving grant + admin, from/to, amount,
reason, resulting balances; also the rolling-24h cap ledger), a stable numeric
`community_permission_grants.grant_id`, and the least-privilege grants the
economy runtime role needs (read the caller's grant, append/settle audit rows).
Idempotent. The DDL lives in `scripts/db/bundle_economy_elevated.sql` so the
shipped schema and the Rust-tested schema cannot drift.

Chained off the single head `0087_bundle_data_v2`; if another open PR
also adds 0088, renumber this file and its `down_revision` on merge.

Revision ID: 0088_economy_elevated
Revises: 0087_bundle_data_v2
Create Date: 2026-11-07
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0088_economy_elevated"
down_revision = "0087_bundle_data_v2"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_economy_elevated.sql"
)
_PHASE_MARKER = "-- @@PHASE: VALIDATE@@"


def _phases() -> tuple[str, str]:
    """Split the DDL into the catalog-only CHECK swap and the ledger-scanning VALIDATE."""
    swap, marker, validate = _SQL_PATH.read_text(encoding="utf-8").partition(_PHASE_MARKER)
    if not marker or not validate.strip():
        raise RuntimeError(f"{_SQL_PATH.name} is missing its {_PHASE_MARKER} phase")
    return swap, validate


def upgrade() -> None:
    """Apply the DDL; swap the kind CHECK as NOT VALID, commit, then VALIDATE it."""
    swap, validate = _phases()
    bind = op.get_bind()
    bind.exec_driver_sql(swap)
    bind.commit()  # release the swap's ACCESS EXCLUSIVE lock before the scan
    bind.exec_driver_sql(validate)


def downgrade() -> None:
    """Drop the audit table and restore the 0071 kind CHECK (fails if new-kind rows exist)."""
    # Revoke the column-scoped SELECT the upgrade granted the economy runtime role on the grants
    # table. Dropping `grant_id` removes only that column's privilege; the rest would linger and
    # make the later `DROP ROLE waddles_economy_runtime` (0051 downgrade) fail with
    # DependentObjectsStillExist.
    op.execute(
        "DO $$ BEGIN IF to_regclass('community_permission_grants') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_economy_runtime') THEN "
        "REVOKE ALL ON community_permission_grants FROM waddles_economy_runtime; "
        "END IF; END $$"
    )
    op.execute("DROP TABLE IF EXISTS economy_elevated_audit")
    op.execute(
        "DO $$ BEGIN IF to_regclass('community_permission_grants') IS NOT NULL THEN "
        "DROP INDEX IF EXISTS uq_community_permission_grants_grant_id; "
        "ALTER TABLE community_permission_grants DROP COLUMN IF EXISTS grant_id; "
        "END IF; END $$"
    )
    op.execute("ALTER TABLE economy_ledger DROP CONSTRAINT IF EXISTS economy_ledger_kind_check")
    op.execute(
        "ALTER TABLE economy_ledger ADD CONSTRAINT economy_ledger_kind_check "
        "CHECK (kind IN ('wager', 'transfer_out', 'transfer_in', 'credit', 'debit', "
        "'cond_debit', 'admin_adjust', 'pot_payout', 'exchange'))"
    )
