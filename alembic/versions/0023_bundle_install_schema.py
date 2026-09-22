"""app_version_uploads, app_install_approvals, app_stream_grants, platform_settings.

Continues migration 0022 with the remaining hub-api-exclusive
control-plane tables this milestone (M2a "hub-api install hooks", spec
Sec16 M2 row) needs: the pre-publish state-machine tracker (spec
Sec9.1), the install-time consent/approval record (spec Sec6.9,
Sec9.7), the resolved stream-grant record (spec Sec6.8, scaffolded here
-- table only, the grant-resolution service of spec Sec9.6/Sec5.2 is a
follow-on out of this milestone's realistic scope), and the global
`bundles.allow_prebuilt` setting (spec Sec9.2, Sec12.3). `custom_platforms`
(plan Task 3's sixth table) is left for a future migration, matching
0022's own scope note.

`app_version_uploads` is hub-api's own pre-publish lifecycle tracker --
`app_versions` (migration 0022) has no status column by design (spec
Sec6.10), so the UPLOADED -> VALIDATING -> ... -> PUBLISHED/REJECTED
state machine (spec Sec9.1) lives here, correlated to `app_versions` by
the natural key `(app_id, version)` and a denormalized `app_version_id`
pointer set once a publisher's row exists.

`approved_by`/`granted_by`/`updated_by` are `INTEGER REFERENCES
hub_users(id)`, not `uuid` -- `hub_users` is this codebase's one
identity table and uses an integer SERIAL key, matching every existing
actor-FK column elsewhere in this schema (PII tokenization: never a
name or email, spec Sec9.7.2).

Grants are rendered from config/postgres/rbac-matrix.yaml, same
generator migration 0022 used, scoped to only the tables created here
so this migration never re-touches 0022's already-correct grants.

Revision ID: 0023_bundle_install_schema
Revises: 0022_app_versions_and_rbac
Create Date: 2026-09-22
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0023_bundle_install_schema"
down_revision = "0022_app_versions_and_rbac"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset(
    {"app_version_uploads", "app_install_approvals", "app_stream_grants", "platform_settings"}
)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0023", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0023"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_version_uploads (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            requested_by INTEGER REFERENCES hub_users(id),
            artifact_kind VARCHAR(20) NOT NULL
                CHECK (artifact_kind IN ('source', 'prebuilt')),
            language VARCHAR(20) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'UPLOADED'
                CHECK (status IN (
                    'UPLOADED', 'VALIDATING', 'SCANNING', 'INSPECTING', 'COMPILING',
                    'ADDRESSING', 'PUBLISHING', 'PUBLISHED', 'REJECTED'
                )),
            reject_reason VARCHAR(100),
            compiler_job_name VARCHAR(255),
            staging_manifest_key VARCHAR(500),
            staging_source_key VARCHAR(500),
            staging_component_key VARCHAR(500),
            manifest_json JSONB,
            app_version_id BIGINT REFERENCES app_versions(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (app_id, version)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_version_uploads IS "
        "'hub-api pre-publish state machine (spec Sec9.1); app_versions has no status column'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_install_approvals (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            permission_hash VARCHAR(71) NOT NULL,
            summary_json JSONB NOT NULL,
            approved_by INTEGER REFERENCES hub_users(id),
            approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            superseded_by BIGINT REFERENCES app_install_approvals(id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_install_approvals_current
            ON app_install_approvals (app_id, version, tenant_id, community_id)
            WHERE superseded_by IS NULL
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_install_approvals.approved_by IS "
        "'hub_users.id -- PII tokenization: never a name or email (spec Sec9.7.2)'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_stream_grants (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            stream_key VARCHAR(500) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            label VARCHAR(255) NOT NULL,
            granted_by INTEGER REFERENCES hub_users(id),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_stream_grants_active
            ON app_stream_grants (app_id, stream_key)
            WHERE revoked_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_stream_grants_lookup "
        "ON app_stream_grants (tenant_id, community_id, app_id) WHERE revoked_at IS NULL"
    )
    op.execute(
        "COMMENT ON TABLE app_stream_grants IS "
        "'Resolved consumes grants (spec Sec6.8) -- table scaffolded here; "
        "the resolution service (Sec9.6/Sec5.2) is a follow-on milestone'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_settings (
            id BIGSERIAL PRIMARY KEY,
            key VARCHAR(150) NOT NULL UNIQUE,
            value TEXT,
            updated_by INTEGER REFERENCES hub_users(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        INSERT INTO platform_settings (key, value)
        VALUES ('bundles.allow_prebuilt', 'true')
        ON CONFLICT (key) DO NOTHING
        """
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE app_version_uploads_id_seq, "
        "app_install_approvals_id_seq, app_stream_grants_id_seq, "
        "platform_settings_id_seq TO hub_api;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_settings")
    op.execute("DROP TABLE IF EXISTS app_stream_grants")
    op.execute("DROP TABLE IF EXISTS app_install_approvals")
    op.execute("DROP TABLE IF EXISTS app_version_uploads")
