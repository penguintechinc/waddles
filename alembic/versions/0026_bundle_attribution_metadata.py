"""app_catalog/app_versions attribution + marketplace metadata columns.

Extends `bundle.yaml` v2 (`hub_api/services/bundle_manifest_v2.py`) and the
older install/available/activate manifest schema
(`libs/flask_core/flask_core/app_manifest.py`) with an optional attribution
block -- `author`, `license` (SPDX id, `flask_core.bundle_attribution`
allowlist), `source_url` (https-only), `alternative_to` (app_ids or
free-text feature names, for the new `category = 'alternatives'`
marketplace listing), `homepage_url` (https-only), and `notice` (inline
text or an in-source `NOTICE` file path) -- so a third-party port can be
credited and its license terms tracked once a bundle is onboarded.

Two tables, two lifecycles:

- `app_catalog` (migration 0022's own FK target, created pre-baseline):
  the marketplace's current/display record for an `app_id`, read by
  `blueprints/v1/marketplace_lifecycle.py`'s `BundleDTO` -- gets the full
  attribution block plus `category`. `license_review_required` is a
  derived, not user-supplied, column: `flask_core.bundle_attribution.
  license_requires_review()` flags a recognized copyleft SPDX id for
  human review rather than silently accepting it (house supply-chain
  policy) -- computed at write time by `install_bundle()` /
  `bundle_version_service._publish_prebuilt_version()`, never trusted
  from client input.
- `app_versions` (migration 0022): a per-published-version attribution
  snapshot (`author`/`license`/`license_review_required`/`source_url`
  only -- no `homepage_url`/`notice`/`alternative_to`/`category`, which
  are app-level, not version-level, concerns), written by
  `_publish_prebuilt_version()` and covered by that table's existing
  `app_versions_audit_log` trigger (migration 0022) for free.

All columns nullable/optional -- a first-party `builtin` bundle predating
this migration reads back with `NULL`s, exactly like every other
`ALTER TABLE ... ADD COLUMN` in this migration history (e.g. 0024's
`component_key`/`sidecar_key`). Enforcement (`author`/`license` mandatory
for a `provider: thirdparty` vendor bundle; SPDX allowlist; https-only
URLs) lives in the two manifest validators, not a DB constraint -- same
precedent as every other manifest-shape rule in this repo (`bundle_
manifest_v2.py`'s own pure-YAML rules, none of which are CHECK
constraints).

Revision ID: 0026_bundle_attribution_metadata
Revises: 0025_app_source_bindings
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op

revision = "0026_bundle_attribution_metadata"
down_revision = "0025_app_source_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the attribution/marketplace metadata columns to `app_catalog` and `app_versions`."""
    op.execute(
        """
        ALTER TABLE app_catalog
            ADD COLUMN IF NOT EXISTS author VARCHAR(255),
            ADD COLUMN IF NOT EXISTS license VARCHAR(50),
            ADD COLUMN IF NOT EXISTS license_review_required BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS source_url TEXT,
            ADD COLUMN IF NOT EXISTS alternative_to JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS homepage_url TEXT,
            ADD COLUMN IF NOT EXISTS notice TEXT,
            ADD COLUMN IF NOT EXISTS category VARCHAR(50)
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_catalog.license_review_required IS "
        "'Set when license is a recognized copyleft SPDX id (flask_core.bundle_attribution) -- "
        "flagged for human review, never silently accepted or auto-rejected'"
    )
    op.execute(
        "COMMENT ON COLUMN app_catalog.category IS "
        "'Marketplace listing category, e.g. alternatives (paired with alternative_to)'"
    )

    op.execute(
        """
        ALTER TABLE app_versions
            ADD COLUMN IF NOT EXISTS author VARCHAR(255),
            ADD COLUMN IF NOT EXISTS license VARCHAR(50),
            ADD COLUMN IF NOT EXISTS license_review_required BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS source_url TEXT
        """
    )


def downgrade() -> None:
    """Drop the attribution/marketplace metadata columns -- inverse of `upgrade()`."""
    op.execute(
        """
        ALTER TABLE app_versions
            DROP COLUMN IF EXISTS author,
            DROP COLUMN IF EXISTS license,
            DROP COLUMN IF EXISTS license_review_required,
            DROP COLUMN IF EXISTS source_url
        """
    )
    op.execute(
        """
        ALTER TABLE app_catalog
            DROP COLUMN IF EXISTS author,
            DROP COLUMN IF EXISTS license,
            DROP COLUMN IF EXISTS license_review_required,
            DROP COLUMN IF EXISTS source_url,
            DROP COLUMN IF EXISTS alternative_to,
            DROP COLUMN IF EXISTS homepage_url,
            DROP COLUMN IF EXISTS notice,
            DROP COLUMN IF EXISTS category
        """
    )
