"""Let the DEK issuance audit table record the `at-rest` lineage (`KeyService.GetTenantDek`).

`keystore.stream_dek_issuances` (0100) audits every broker decision. The broker now also serves
the tenant at-rest key lineage (`GetTenantDek`, per-tenant sealing of bundle kv values), so the
`purpose` CHECK is widened from `('ingest-stream')` to `('ingest-stream', 'at-rest')`. No data
change, no new table: one audit trail for every DEK that leaves hub-api.

This revision is also the merge point of the two heads present on `release/v3.0.X`
(`0108_bundle_precompile_requests` and `0109_platform_vault`), restoring a single alembic head.

Revision ID: 0110_dek_issuances_at_rest
Revises: 0109_platform_vault, 0108_bundle_precompile_requests
Create Date: 2026-11-15
"""

from __future__ import annotations

from alembic import op

revision = "0110_dek_issuances_at_rest"
down_revision = ("0109_platform_vault", "0108_bundle_precompile_requests")
branch_labels = None
depends_on = None

_TABLE = "keystore.stream_dek_issuances"
_CONSTRAINT = "ck_stream_dek_issuances_purpose"

_DROP_PURPOSE_CHECKS = """
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT con.conname
        FROM pg_constraint con
        WHERE con.conrelid = 'keystore.stream_dek_issuances'::regclass
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%purpose%'
          AND pg_get_constraintdef(con.oid) NOT LIKE '%outcome%'
    LOOP
        EXECUTE format('ALTER TABLE keystore.stream_dek_issuances DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$
"""


def upgrade() -> None:
    """Replace the purpose CHECK (whatever Postgres named it) with the two-lineage one."""
    op.execute(_DROP_PURPOSE_CHECKS)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (purpose IS NULL OR purpose IN ('ingest-stream', 'at-rest'))"
    )


def downgrade() -> None:
    """Restore the single-lineage CHECK for new rows (NOT VALID: audit rows are never deleted)."""
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (purpose IN ('ingest-stream')) NOT VALID"
    )
