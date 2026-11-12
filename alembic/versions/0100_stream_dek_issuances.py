"""GetStreamDek audit trail: `keystore.stream_dek_issuances` (hub-api only, append-only).

One row per `KeyService.GetStreamDek` decision (#442) -- issued AND refused. The broker only
returns a sealed DEK after its `issued` row commits, so every DEK that left hub-api is
accounted for. No key material, sealed bytes, caller public key or PII is stored: numeric
tenant id, lineage, DEK version, caller service name (SPIFFE last segment) and an int outcome.

`outcome` int-enum (services/stream_dek_broker.py OUTCOME_*):
1 issued, 2 denied_service, 3 denied_tenant, 4 invalid, 5 not_found, 6 unavailable.

`tenant_id` is nullable with no FK on purpose: a refusal before the tenant id parsed (or for a
non-existent tenant) is still recorded, and audit rows must outlive tenant deletion.

The stream DEKs themselves live in `keystore.tenant_encryption_keys` (purpose='ingest-stream',
created by 0054) -- no schema change needed there.

Revision ID: 0100_stream_dek_issuances
Revises: 0099_discord_role_mirror
Create Date: 2026-11-11
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0100_stream_dek_issuances"
down_revision = "0099_discord_role_mirror"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"keystore.stream_dek_issuances"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0100", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0100"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create the audit table + index and apply its RBAC-matrix grants."""
    op.execute("CREATE SCHEMA IF NOT EXISTS keystore")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS keystore.stream_dek_issuances (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER,
            purpose VARCHAR(30) CHECK (purpose IN ('ingest-stream')),
            dek_version INTEGER CHECK (dek_version IS NULL OR dek_version >= 1),
            caller_service VARCHAR(64),
            outcome SMALLINT NOT NULL CHECK (outcome BETWEEN 1 AND 6),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_stream_dek_issued_complete CHECK (
                outcome <> 1 OR (tenant_id IS NOT NULL AND purpose IS NOT NULL
                                 AND dek_version IS NOT NULL AND caller_service IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE keystore.stream_dek_issuances IS "
        "'Append-only audit of KeyService.GetStreamDek decisions. No key material. "
        "outcome: 1 issued, 2 denied_service, 3 denied_tenant, 4 invalid, 5 not_found, "
        "6 unavailable.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_stream_dek_issuances_tenant_time "
        "ON keystore.stream_dek_issuances (tenant_id, created_at DESC)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    op.execute("GRANT USAGE ON SCHEMA keystore TO hub_api, migration_runner;")
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)
    op.execute("GRANT USAGE ON SEQUENCE keystore.stream_dek_issuances_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the audit table (the stream DEKs in tenant_encryption_keys are untouched)."""
    op.execute("DROP TABLE IF EXISTS keystore.stream_dek_issuances")
