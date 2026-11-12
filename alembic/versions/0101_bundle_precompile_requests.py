"""Bundle AOT precompile request queue (hub-api only).

`bundle_precompile_requests` -- one row per approved+signed bundle artifact digest that still
needs an ahead-of-time `.cwasm` build. hub-api enqueues on approval/signing (INSERT ... ON
CONFLICT (artifact_digest) DO NOTHING, so a digest is enqueued at most once while pending); the
`executor-precompile` CronJob claims rows under a lease (`claimed_at`, `attempts`), runs
`bundle-executor --precompile`, and deletes rows the precompiler reports as compiled/skipped.
Per-engine-fingerprint idempotency lives in object storage (`precompiled/<engine_fp>/...`), not
here. Rows that hit the attempt cap stay with `last_error` set for operators (dead letter).

Revision ID: 0101_bundle_precompile_requests
Revises: 0100_stream_dek_issuances
Create Date: 2026-11-11
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0101_bundle_precompile_requests"
down_revision = "0100_stream_dek_issuances"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"bundle_precompile_requests"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location(
        "waddles_rbac_matrix_0101", _MATRIX_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0101"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create the queue table, its claim index, and apply the RBAC grants."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_precompile_requests (
            id BIGSERIAL PRIMARY KEY,
            artifact_digest VARCHAR(80) NOT NULL,
            app_id VARCHAR(255) NOT NULL,
            version VARCHAR(64) NOT NULL,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error VARCHAR(500),
            CONSTRAINT uq_bundle_precompile_requests_digest UNIQUE (artifact_digest),
            CONSTRAINT ck_bundle_precompile_requests_attempts CHECK (attempts >= 0)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE bundle_precompile_requests IS "
        "'Pending AOT precompile work per approved bundle digest. hub-api enqueues; the "
        "executor-precompile CronJob claims (lease) and acks.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_precompile_requests_claim "
        "ON bundle_precompile_requests (claimed_at, attempts)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)
    op.execute("GRANT USAGE ON SEQUENCE bundle_precompile_requests_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the queue table (pending requests are re-creatable by the backfill Job)."""
    op.execute("DROP TABLE IF EXISTS bundle_precompile_requests")
