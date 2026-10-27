"""`app_global_installs` + `bundle_tenant_availability` -- the App Bundle 3-tier split (M2b).

Coordinator ruling (2026-09-27): `bundle_approval_service.py::approve_version()`
conflated all three authorization tiers into one `platform:admin`-gated call
(approve a version + activate it for a `(tenant, community)` in the same
transaction). This milestone splits it into three independently-scoped
steps, matching the discipline the OLDER `AppManifest`-based marketplace
already has (`app_catalog` -> `app_tenant_availability` -> `app_activations`,
migration 069) -- this migration brings the newer WASM-component pipeline
(`app_version_uploads` -> `app_versions` -> `app_install_approvals` +
`app_active_versions`, migrations 0022-0023) up to the same shape:

  1. GLOBAL (`platform:admin`): install a version into the platform catalog.
     `app_global_installs` -- one row per (app_id, version) install event,
     `superseded_by`-chained the same way `app_install_approvals` already
     is. Vendor bundles require a human `platform:admin`; first-party
     `waddles.core.*` bundles use `install_source='system:core-seeder'`
     with `installed_by=NULL` (mirrors `app_install_approvals.
     approval_source`, migration 0026). NO tenant/community activation
     happens here anymore.

  2. TENANT (tenant-admin scope): `bundle_tenant_availability` -- whether a
     globally-installed app is visible/enable-able in one tenant's
     marketplace. Deliberately NOT named `app_tenant_availability` --
     that name is already taken by migration 069's older, differently-
     shaped table (`id`/`config_defaults`, no `pinned_version_id`/
     `updated_by`) for the unrelated `AppManifest` pipeline; reusing it
     here would silently conflate two independent systems that happen to
     share `app_catalog` as their one common FK target.

  3. COMMUNITY: `app_install_approvals`/`app_active_versions` (both
     pre-existing, migrations 0022-0023) are UNCHANGED in shape -- only
     `bundle_approval_service.py`'s application code moves the activation
     write (previously inside `approve_version()`) into a new
     `activate_for_community()`, gated on the caller's community-admin
     membership instead of `platform:admin`, and requiring a current
     `bundle_tenant_availability` row instead of "the caller happens to
     hold platform:admin". `community_id` is no longer optional at this
     tier (no more tenant-wide `TENANT_WIDE_COMMUNITY_SENTINEL` activation
     path for NEW writes) -- every activation is scoped to one real
     community. `core/bundle_active_set/src/query.rs`'s read-side join
     (`app_active_versions` JOIN `app_versions` JOIN `app_install_
     approvals` on matching `community_id`) is unchanged by this
     migration; verified by inspection (query.rs:264-292) rather than a
     schema change, since neither table's columns move.

Revision ID: 0027_app_bundle_three_tier
Revises: 0026_app_install_approval_source
Create Date: 2026-09-27
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0027_app_bundle_three_tier"
down_revision = "0026_app_install_approval_source"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"app_global_installs", "bundle_tenant_availability"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0027", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0027"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_global_installs (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            version_id BIGINT NOT NULL REFERENCES app_versions(id),
            permission_hash VARCHAR(71) NOT NULL,
            summary_json JSONB NOT NULL,
            install_source VARCHAR(50) NOT NULL DEFAULT 'human'
                CHECK (install_source IN ('human', 'system:core-seeder')),
            installed_by INTEGER REFERENCES hub_users(id),
            installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_by INTEGER REFERENCES hub_users(id),
            revoked_at TIMESTAMPTZ,
            superseded_by BIGINT REFERENCES app_global_installs(id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_global_installs_current
            ON app_global_installs (app_id, version)
            WHERE superseded_by IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_global_installs_app_current "
        "ON app_global_installs (app_id) WHERE superseded_by IS NULL AND revoked_at IS NULL"
    )
    op.execute(
        "COMMENT ON TABLE app_global_installs IS "
        "'GLOBAL tier (platform:admin): one row per (app_id, version) platform-catalog "
        "install event, superseded_by-chained like app_install_approvals -- see this "
        "migration''s own module docstring for the full 3-tier split'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_tenant_availability (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            available BOOLEAN NOT NULL DEFAULT TRUE,
            pinned_version_id BIGINT REFERENCES app_versions(id),
            updated_by INTEGER REFERENCES hub_users(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, app_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE bundle_tenant_availability IS "
        "'TENANT tier: whether a globally-installed app is enabled in one tenant''s "
        "marketplace -- NOT app_tenant_availability (migration 069), a differently-shaped "
        "table for the older AppManifest pipeline'"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE app_global_installs_id_seq TO hub_api, migration_runner;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bundle_tenant_availability")
    op.execute("DROP TABLE IF EXISTS app_global_installs")
