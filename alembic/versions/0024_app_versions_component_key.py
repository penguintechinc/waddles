"""`app_versions.component_key`/`sidecar_key` -- the staged MinIO object keys (spec Sec6.10/9.1).

Data-plane/hub-api seam contract (coordinator-mandated, both halves must
agree exactly): the pre-built-component staging path
(`services/storage_service.py::upload_bundle_component()`,
`services/bundle_version_service.py::process_prebuilt_component()`)
already computes and uploads to the exact key
`bundles/{app_id}/{version}/{sha256}.wasm` (`storage_service.
bundle_component_key()`), plus a `.json` sidecar at the same path
stem. Until now neither key was ever persisted on `app_versions` --
`app_version_uploads.staging_component_key` carried the `.wasm` key only,
scoped to the pre-publish tracker, not the published digest table the
data-plane loader actually reads. `component_key`/`sidecar_key` let the
data-plane loader (and any other consumer) resolve a published version's
staged bytes directly from `app_versions`, with zero digest-to-key
derivation logic duplicated on that side.

Both columns are nullable TEXT: `component_key` is populated for every
`artifact_kind = 'prebuilt'` row going forward (written by
`process_prebuilt_component()` at the same ADDRESSING step that already
uploads the bytes); `sidecar_key` likewise. Neither applies to
`artifact_kind = 'source'` rows (the compiler-Job path, still a
not-yet-built follow-on per this milestone's own scope notes) -- NULL is
the correct, permanent state there, not a backfill gap.

Revision ID: 0024_app_versions_component_key
Revises: 0023_bundle_install_schema
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op

revision = "0024_app_versions_component_key"
down_revision = "0023_bundle_install_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app_versions
          ADD COLUMN IF NOT EXISTS component_key TEXT,
          ADD COLUMN IF NOT EXISTS sidecar_key TEXT
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_versions.component_key IS "
        "'Full MinIO object key of the staged .wasm: bundles/{app_id}/{version}/{sha256}.wasm "
        "(storage_service.bundle_component_key()) -- set at ADDRESSING for prebuilt artifacts'"
    )
    op.execute(
        "COMMENT ON COLUMN app_versions.sidecar_key IS "
        "'The .json sidecar key at the same path stem as component_key; "
        "NULL for artifact_kind=source'"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app_versions
          DROP COLUMN IF EXISTS sidecar_key,
          DROP COLUMN IF EXISTS component_key
        """
    )
