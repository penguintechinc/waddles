"""app_versions (the digest table, spec Sec6.10) + app_active_versions + audit trigger.

M2a "hub-api install hooks" (spec Sec16 M2 row, Sec9.1/Sec9.2/Sec9.7):
continues migration 0020's own scope note -- the full M2b/M2a plan
(`docs/superpowers/plans/2026-09-14-rust-data-plane-m2b-hub-api.md`,
Tasks 2-3) lays out nine new control-plane tables; 0020 carried forward
only `ingest_sources` + the Postgres roles. This migration adds the two
`app_versions`/`app_active_versions` state-machine tables (spec
Sec6.10); migration 0023 adds the remaining install/approval tables
this milestone needs. `custom_platforms` is left for a future
migration (out of this milestone's scope, per the task's own realistic-
scope note) -- `render_create_roles_sql()` and `render_grant_sql()` are
both idempotent/table-scoped, so that future migration is a no-op here,
not a conflict.

`app_versions` is written by exactly two roles -- `waddles_publisher`
(the trusted publisher container, M2's own future deliverable; not yet
built, but the role and grants are provisioned now so its migration
needs no RBAC follow-up) and `hub_api` (owns scan outcome, approval
linkage, and the digest cross-check of spec Sec9.4). Every other
service role gets zero privileges, and every write is captured by an
AFTER-trigger recording the writing role and the old/new digest (spec
Sec6.10, D28).

Grants are rendered from config/postgres/rbac-matrix.yaml at migration
-run time via scripts/db/rbac_matrix.py -- this file contains no
hand-written GRANT statement. The matrix file already carries explicit
rows for every table this migration and 0023 create (added ahead of
time by the M2b planning pass); `render_grant_sql(rows, tables=...)`
scopes rendering to only the tables this migration itself creates.

`app_active_versions` is the separate, hub-api-owned pointer table:
activation and rollback are an UPDATE of `version_id` here, never an
edit of a digest row (spec Sec6.10). Wiring activation/rollback into a
`bundle_activation_service.py` is a follow-on (spec Sec9.5, plan Task
16) out of this milestone's realistic scope; the table is created now
so that follow-on has nothing left to migrate.

Revision ID: 0022_app_versions_and_rbac
Revises: 0021_workstreams_and_usage
Create Date: 2026-09-22
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0022_app_versions_and_rbac"
down_revision = "0021_workstreams_and_usage"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"app_versions", "app_active_versions", "app_versions_audit_log"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0022", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Register in sys.modules BEFORE exec_module() -- see migration 0020's
    # identical comment: rbac_matrix.py's @dataclass(slots=True,
    # frozen=True) classes resolve their own module via sys.modules during
    # class creation; an unregistered module crashes that lookup.
    sys.modules["waddles_rbac_matrix_0022"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_versions (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            artifact_digest VARCHAR(71),
            cwasm_digest VARCHAR(71),
            wasmtime_abi VARCHAR(50),
            collector VARCHAR(20),
            size_bytes BIGINT,
            language VARCHAR(20) NOT NULL,
            artifact_kind VARCHAR(20) NOT NULL
                CHECK (artifact_kind IN ('source', 'prebuilt')),
            built_at TIMESTAMPTZ,
            builder VARCHAR(100),
            scan_status VARCHAR(30) NOT NULL DEFAULT 'not_scanned'
                CHECK (scan_status IN (
                    'scanned', 'scanned_with_findings', 'not_scanned', 'scan_failed'
                )),
            badge VARCHAR(100),
            approval_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (app_id, version),
            UNIQUE (artifact_digest)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_versions IS "
        "'The digest table (spec Sec6.10) -- exactly two writers: waddles_publisher and hub_api'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_active_versions (
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER NOT NULL DEFAULT 0,
            version_id BIGINT NOT NULL REFERENCES app_versions(id),
            activated_by INTEGER REFERENCES hub_users(id),
            activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (app_id, tenant_id, community_id)
        )
        """
    )
    # Postgres composite PK columns are implicitly NOT NULL, so
    # community_id can never carry the usual "tenant-wide" NULL. 0 is the
    # reserved sentinel instead (communities.id is a real SERIAL starting
    # at 1, so it is never 0) -- the same convention the distribution
    # service (spec Sec6.7) already applies to the `_tenant` key segment.
    op.execute(
        "COMMENT ON COLUMN app_active_versions.community_id IS "
        "'0 = tenant-wide (sentinel; communities.id never = 0), matching the _tenant convention'"
    )
    op.execute(
        "COMMENT ON TABLE app_active_versions IS "
        "'hub-api-owned activation pointer -- rollback updates version_id, never edits a digest'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_versions_audit_log (
            id BIGSERIAL PRIMARY KEY,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            db_role VARCHAR(100) NOT NULL,
            operation VARCHAR(10) NOT NULL,
            app_id VARCHAR(255) NOT NULL,
            version VARCHAR(50) NOT NULL,
            old_digest VARCHAR(71),
            new_digest VARCHAR(71)
        )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_app_versions_audit() RETURNS trigger AS $$
        BEGIN
            INSERT INTO app_versions_audit_log
                (db_role, operation, app_id, version, old_digest, new_digest)
            VALUES (
                session_user,
                TG_OP,
                COALESCE(NEW.app_id, OLD.app_id),
                COALESCE(NEW.version, OLD.version),
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.artifact_digest END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.artifact_digest END
            );
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_app_versions_audit ON app_versions;
        CREATE TRIGGER trg_app_versions_audit
            AFTER INSERT OR UPDATE OR DELETE ON app_versions
            FOR EACH ROW EXECUTE FUNCTION fn_app_versions_audit()
        """
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    roles = sorted(matrix_module.matrix_roles(matrix_path))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_create_roles_sql(roles):
        op.execute(statement)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    # Sequence usage must be granted alongside table INSERT or the two
    # writer roles cannot obtain a new id.
    op.execute("GRANT USAGE ON SEQUENCE app_versions_id_seq TO waddles_publisher, hub_api;")
    op.execute("GRANT USAGE ON SEQUENCE app_versions_audit_log_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_app_versions_audit ON app_versions")
    op.execute("DROP FUNCTION IF EXISTS fn_app_versions_audit()")
    op.execute("DROP TABLE IF EXISTS app_versions_audit_log")
    op.execute("DROP TABLE IF EXISTS app_active_versions")
    op.execute("DROP TABLE IF EXISTS app_versions")
