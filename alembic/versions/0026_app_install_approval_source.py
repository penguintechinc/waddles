"""`app_install_approvals.approval_source` -- distinguishes a human approval from a SYSTEM one.

Core-bundle-seeder milestone: first-party `waddles.core.*` bundles must
activate at deploy time under the platform's SYSTEM identity, with NO
human global-admin approval step (global-admin approval stays reserved
for third-party vendor bundles, per `services/vendor_bundle_authz.py`'s
own `CORE_NAMESPACE_PREFIX` reservation). `app_install_approvals.
approved_by` (migration 0023) is already a NULLABLE `INTEGER REFERENCES
hub_users(id)` -- no schema change is needed to let a SYSTEM-seeded row
carry `approved_by = NULL` instead of a fake `hub_users` row (explicitly
never a fake user, per Justin's ruling on this milestone).

What migration 0023 has no column for is a way to tell a SYSTEM-seeded
approval apart from a human one at read time (an audit/reporting need,
not a write-path requirement) -- `approval_source` fills exactly that
gap. `'human'` is the default for every existing and future row written
by the human-gated `POST /apps/{app_id}/versions/approve` path
(`services/bundle_approval_service.py::approve_version()`'s existing
callers are unaffected -- the parameter is optional and defaults to
`'human'`); `'system:core-seeder'` is written only by
`hub_api/cli/seed_core_bundles.py`. The CHECK constraint is a small,
explicit enum rather than a free-text column -- deliberately narrow so a
future actor (e.g. a second automated seeder) is an explicit migration
decision, not a silent new string value.

Revision ID: 0026_app_install_approval_source
Revises: 0025_app_source_bindings
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op

revision = "0026_app_install_approval_source"
down_revision = "0025_app_source_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app_install_approvals
          ADD COLUMN IF NOT EXISTS approval_source VARCHAR(50) NOT NULL DEFAULT 'human'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_app_install_approvals_approval_source'
                  AND connamespace = 'public'::regnamespace
            ) THEN
                ALTER TABLE app_install_approvals
                  ADD CONSTRAINT ck_app_install_approvals_approval_source
                  CHECK (approval_source IN ('human', 'system:core-seeder'));
            END IF;
        END $$
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_install_approvals.approval_source IS "
        "'human (default, global-admin approved) or system:core-seeder "
        "(first-party waddles.core.* bundle, seeded at deploy time, no human approval)'"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_app_install_approvals_approval_source'
                  AND connamespace = 'public'::regnamespace
            ) THEN
                ALTER TABLE app_install_approvals
                  DROP CONSTRAINT ck_app_install_approvals_approval_source;
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE app_install_approvals DROP COLUMN IF EXISTS approval_source")
