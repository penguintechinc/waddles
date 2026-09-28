"""Grant storage for the Android-style permission catalog (bundle permissions & capability gate spec).

`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
Sec4 (grant storage) + Sec12 Phase 0's "Migration:" task. hub-api is the
only RW path -- the data plane (`svc_process`/`svc_action`) holds a
read-only role against a read replica (`waddles_bundle_reader`, same
pattern PR #415/migration 0025's own module docstring establishes), so
grants are control-plane metadata living in the existing control-plane
DB, never `bundle-data`.

Five tables (spec Sec4's table):

- `app_permission_requests` -- GLOBAL tier ceiling. Written by
  `bundle_permission_service.record_permission_requests()`
  (`bundle_approval_service.approve_version()`'s catalog-approval step,
  or `seed_core_bundles.py`'s system-actor path, Sec3.6). One row per
  `(app_id, version, permission_id)` -- the maximal grant set every lower
  tier can only narrow.
- `app_tenant_permission_restrictions` -- TENANT tier. Presence of a row
  means "restricted" (excluded) -- an opt-out list, never an opt-in one
  (Sec3.2).
- `community_permission_grants` -- COMMUNITY tier. The row the data plane
  actually reads at runtime (Sec3.3) -- `(community_id, app_id,
  permission_id)` PK, community's own bound within the approved ceiling.
- `app_permission_grant_versions` -- append-only version-pinning ledger
  (Sec3.4/Sec4's versioning note) -- the row `classify_permission_diff()`
  reads to decide whether a community has re-consented for a given
  version, and the source of the monotonic per-(tenant, community, app)
  grant-version counter the push-invalidation event carries.
- `bundle_reputation_adjustments` -- Sec7's audit ledger (out of this
  slice's endpoint scope, Phase 10; table created now since Sec4 lists it
  alongside the other four grant-storage tables and a later phase should
    not need its own schema migration to start writing to it).

This migration renumbers against whichever `0031_*` lands first on
`release/v3.0.X` (`feature/bundle-artifact-signing`, not yet merged onto
this branch's base) -- `down_revision` points at
`0030_bundle_app_schemas`, the latest revision actually present on
`feature/bundle-app-schemas` at the time this migration was authored; a
follow-up rebase/renumbering commit will retarget `down_revision` to
`0031_bundle_artifact_signature` once both branches merge, per this PR's
own description.

Revision ID: 0032_bundle_permission_grants
Revises: 0030_bundle_app_schemas
Create Date: 2026-09-28
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0032_bundle_permission_grants"
down_revision = "0030_bundle_app_schemas"
branch_labels = None
depends_on = None

_TABLES = (
    "app_permission_requests",
    "app_tenant_permission_restrictions",
    "community_permission_grants",
    "app_permission_grant_versions",
    "bundle_reputation_adjustments",
)

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset(_TABLES)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0032", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0032"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module

#: Not part of the hub-api-owned RBAC matrix (config/postgres/rbac-matrix.yaml)
#: -- the Rust data-plane's own read-only role, provisioned by a separate
#: migration (0025/0028's own precedent). Guarded by IF EXISTS so this
#: migration never depends on ordering against that one.
_BUNDLE_READER_ROLE = "waddles_bundle_reader"

#: The data plane's read path (spec Sec4) only ever needs the current
#: grant set + the version ledger it compares against -- never the raw
#: tenant-restriction exclusion list or the reputation audit ledger
#: (hub-api-internal), so only these three get the reader grant.
_BUNDLE_READER_TABLES = (
    "app_permission_requests",
    "community_permission_grants",
    "app_permission_grant_versions",
)


def _bundle_reader_grant_sql(table: str) -> str:
    """Guarded `GRANT SELECT ON {table} TO waddles_bundle_reader` -- role AND table must both exist."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}')\n"
        f"     AND to_regclass('{table}') IS NOT NULL THEN\n"
        f"    GRANT SELECT ON {table} TO {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def _bundle_reader_revoke_sql(table: str) -> str:
    """Guarded `REVOKE SELECT ON {table} FROM waddles_bundle_reader` -- the `upgrade()` grant's inverse."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}')\n"
        f"     AND to_regclass('{table}') IS NOT NULL THEN\n"
        f"    REVOKE SELECT ON {table} FROM {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_permission_requests (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            permission_id VARCHAR(255) NOT NULL,
            risk VARCHAR(20) NOT NULL CHECK (risk IN ('normal', 'dangerous')),
            params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            justification TEXT NOT NULL,
            approved_by INTEGER REFERENCES hub_users(id),
            approval_source VARCHAR(50) NOT NULL DEFAULT 'human'
                CHECK (approval_source IN ('human', 'system:core-seeder')),
            approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (app_id, version, permission_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_permission_requests_app_version "
        "ON app_permission_requests (app_id, version)"
    )
    op.execute(
        "COMMENT ON TABLE app_permission_requests IS "
        "'GLOBAL tier ceiling (spec Sec3.1/Sec4): the maximal permission grant set "
        "approved for one (app_id, version) -- every lower tier can only narrow it'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_tenant_permission_restrictions (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            permission_id VARCHAR(255) NOT NULL,
            restricted_by INTEGER REFERENCES hub_users(id),
            restricted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, app_id, permission_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_tenant_permission_restrictions IS "
        "'TENANT tier (spec Sec3.2): presence of a row means the tenant admin has EXCLUDED "
        "this permission tenant-wide -- an opt-out list, never an opt-in one'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS community_permission_grants (
            community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            permission_id VARCHAR(255) NOT NULL,
            params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            granted_by INTEGER REFERENCES hub_users(id),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_by INTEGER REFERENCES hub_users(id),
            revoked_at TIMESTAMPTZ,
            PRIMARY KEY (community_id, app_id, permission_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_community_permission_grants_lookup "
        "ON community_permission_grants (tenant_id, community_id, app_id) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "COMMENT ON TABLE community_permission_grants IS "
        "'COMMUNITY tier (spec Sec3.3/Sec4): the row the data plane actually reads at "
        "runtime -- a community admin''s actual per-permission consent'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_permission_grant_versions (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            grant_version INTEGER NOT NULL,
            permission_snapshot_hash VARCHAR(71) NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_permission_grant_versions_current "
        "ON app_permission_grant_versions (tenant_id, community_id, app_id, grant_version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_permission_grant_versions_lookup "
        "ON app_permission_grant_versions (tenant_id, community_id, app_id, effective_at DESC)"
    )
    op.execute(
        "COMMENT ON TABLE app_permission_grant_versions IS "
        "'Append-only version-pinning ledger (spec Sec3.4/Sec4): the per-(tenant, community, "
        "app) monotonic grant_version counter the push-invalidation event carries, and the "
        "row a re-consent check reads to know whether a community is current for a version'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_reputation_adjustments (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
            target_user_uuid VARCHAR(36) NOT NULL,
            scope VARCHAR(20) NOT NULL CHECK (scope IN ('community', 'tenant')),
            delta INTEGER NOT NULL,
            reason_code VARCHAR(100) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reversal_of BIGINT REFERENCES bundle_reputation_adjustments(id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_reputation_adjustments_app_day "
        "ON bundle_reputation_adjustments (app_id, occurred_at)"
    )
    op.execute(
        "COMMENT ON TABLE bundle_reputation_adjustments IS "
        "'Sec7 audit ledger -- every bundle-originated reputation.adjust() call, "
        "unconditionally, regardless of RPC success/failure; never a destructive delete, "
        "a reversal is a new row with reversal_of set'"
    )

    op.execute("GRANT USAGE ON SEQUENCE app_permission_requests_id_seq TO hub_api, migration_runner")
    op.execute(
        "GRANT USAGE ON SEQUENCE app_permission_grant_versions_id_seq TO hub_api, migration_runner"
    )
    op.execute(
        "GRANT USAGE ON SEQUENCE bundle_reputation_adjustments_id_seq TO hub_api, migration_runner"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    for table in _BUNDLE_READER_TABLES:
        op.execute(_bundle_reader_grant_sql(table))


def downgrade() -> None:
    for table in _BUNDLE_READER_TABLES:
        op.execute(_bundle_reader_revoke_sql(table))
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
