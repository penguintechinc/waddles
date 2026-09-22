"""ingest_sources (spec Sec10.3) + Least User Access RBAC roles (spec D28).

**Scope note (M2b workstreams/usage slice).** The full M2b hub-api plan
(`docs/superpowers/plans/2026-09-14-rust-data-plane-m2b-hub-api.md`)
lays out nine new control-plane tables across two migrations (its own
Tasks 2-3: `app_versions`, `app_active_versions`, `app_versions_audit_log`,
`app_version_uploads`, `app_install_approvals`, `app_stream_grants`,
`custom_platforms`, `ingest_sources`, `platform_settings`) plus the
Postgres roles every later migration's GRANT targets. This migration
carries forward only the two pieces the workstreams/usage-metering slice
(spec Sec5.11/Sec5.12, D30/D31) actually depends on: the roles (needed
by every GRANT statement, including this milestone's own) and
`ingest_sources` (the table `workstreams` is 1:1 with, spec Sec6.11).
The other eight bundle-install tables are out of this slice's scope and
land with a future migration that continues the plan's Task 2/3 --
`render_create_roles_sql()` is idempotent (guarded by a `pg_roles`
existence check), so that future migration re-running it is a no-op,
not a conflict.

Grants are rendered from config/postgres/rbac-matrix.yaml at migration
-run time via scripts/db/rbac_matrix.py -- this file contains no
hand-written GRANT statement (spec D28).

Revision ID: 0020_ingest_sources_rbac
Revises: 0019_kick_app
Create Date: 2026-09-22

Note: the id is abbreviated to `0020_ingest_sources_rbac` (not the more
descriptive `..._and_rbac_roles`) to stay under alembic_version.
version_num's VARCHAR(32) column -- the same constraint that already
shortened 0011's `communities_license_cols` in this same versions/
directory.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0020_ingest_sources_rbac"
down_revision = "0019_kick_app"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"ingest_sources"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0020", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Register in sys.modules BEFORE exec_module(): rbac_matrix.py's
    # @dataclass(slots=True, frozen=True) classes resolve their own
    # module via sys.modules.get(cls.__module__) during class creation
    # (dataclasses._is_type's KW_ONLY sentinel check) -- an unregistered
    # module makes that lookup return None and crashes on `.__dict__`.
    sys.modules["waddles_rbac_matrix_0020"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_sources (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            label VARCHAR(255) NOT NULL,
            secret_ciphertext BYTEA,
            secret_iv BYTEA,
            mapping JSONB,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, platform, source_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE ingest_sources IS "
        "'Per-tenant generic webhook/REST intake registry (spec Sec10.3). "
        "One row per configured source; workstreams (D30) is 1:1 with it.'"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    roles = sorted(matrix_module.matrix_roles(matrix_path))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_create_roles_sql(roles):
        op.execute(statement)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE ingest_sources_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingest_sources")
    for role in (
        "hub_api", "waddles_publisher", "svc_ingest", "svc_process",
        "svc_action", "svc_streaming", "webui", "migration_runner",
    ):
        op.execute(
            # role comes only from the fixed tuple above, never user input;
            # Postgres DDL identifiers (role names) cannot be bind parameters.
            f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608
            f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
            f"    DROP ROLE {role};\n"
            f"  END IF;\n"
            f"EXCEPTION WHEN dependent_objects_still_exist THEN\n"
            f"  NULL; -- role still owns objects from a later migration; leave it\n"
            f"END $$;"
        )
