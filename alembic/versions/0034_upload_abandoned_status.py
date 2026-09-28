"""`app_version_uploads` self-healing stall recovery -- ABANDONED terminal state + partial uniqueness.

**Bug this closes.** A crashed/killed mid-run left `app_version_uploads`
stuck in a non-terminal state (e.g. `INSPECTING`) forever -- every later
run of `hub_api/cli/seed_core_bundles.py` (or a vendor's own re-upload,
`services/bundle_version_service.py::create_version()`) then hit the
table's `UNIQUE (app_id, version)` constraint and returned 409, requiring
a manual DB delete. This migration makes two changes so the application
layer (`services/bundle_version_service.py`'s new `STATUS_ABANDONED` +
`abandon_stalled_upload()`, `hub_api/cli/seed_core_bundles.py`'s new
lease-based reclaim) can self-heal without ever raw-deleting a row:

1. `status` CHECK constraint gains `'ABANDONED'` -- a distinct terminal
   state from `REJECTED` (a validation failure) for "this upload was
   auto-reclaimed because its lease expired", so an operator/audit-log
   reader can tell the two apart.
2. `UNIQUE (app_id, version)` (blocks EVERY re-submission of a version
   that ever had a row, including a terminal-failed one) is replaced by
   a PARTIAL unique index scoped to non-terminal-failure statuses --
   `REJECTED`/`ABANDONED` rows are excluded, so a fresh upload for the
   same `(app_id, version)` can proceed once the prior attempt reaches
   one of those two terminal-failure states; a second concurrent/still-
   in-flight or already-`PUBLISHED` row still blocks, unchanged.

The application layer, not this migration, decides WHEN a non-terminal
row is safe to abandon (lease staleness for the core-bundle-seeder's
SYSTEM-actor path; an explicit `platform:admin` action for a stuck
vendor upload) -- this migration only makes the resulting state
representable and the resubmission path possible.

Revision ID: 0034_upload_abandoned_status
Revises: 0033_hub_users_identity_uuid
Create Date: 2026-09-28

NOTE (queued-migration renumbering, 2026-09-28): this PR branched before
0027-0033 (seeder/lifecycle/changelog/attribution/app-schemas/signing/
grants/users.uuid) merged, so `down_revision` below is a forward
reference to a revision ID that does not exist on this branch yet --
verify it still matches 0033's actual final revision ID at merge time
and rebase this migration onto whatever lands immediately before it.
"""

from __future__ import annotations

from alembic import op

revision = "0034_upload_abandoned_status"
down_revision = "0033_hub_users_identity_uuid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app_version_uploads DROP CONSTRAINT IF EXISTS app_version_uploads_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE app_version_uploads
          ADD CONSTRAINT app_version_uploads_status_check
          CHECK (status IN (
              'UPLOADED', 'VALIDATING', 'SCANNING', 'INSPECTING', 'COMPILING',
              'ADDRESSING', 'PUBLISHING', 'PUBLISHED', 'REJECTED', 'ABANDONED'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE app_version_uploads
          DROP CONSTRAINT IF EXISTS app_version_uploads_app_id_version_key
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_version_uploads_active
            ON app_version_uploads (app_id, version)
            WHERE status NOT IN ('REJECTED', 'ABANDONED')
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_version_uploads.status IS "
        "'spec Sec9.1 pre-publish state machine, plus ABANDONED (auto-reclaimed stale lease, "
        "see services/bundle_version_service.py::abandon_stalled_upload())'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_app_version_uploads_active")
    op.execute(
        """
        ALTER TABLE app_version_uploads
          ADD CONSTRAINT app_version_uploads_app_id_version_key UNIQUE (app_id, version)
        """
    )
    op.execute(
        "ALTER TABLE app_version_uploads DROP CONSTRAINT IF EXISTS app_version_uploads_status_check"
    )
    op.execute(
        """
        ALTER TABLE app_version_uploads
          ADD CONSTRAINT app_version_uploads_status_check
          CHECK (status IN (
              'UPLOADED', 'VALIDATING', 'SCANNING', 'INSPECTING', 'COMPILING',
              'ADDRESSING', 'PUBLISHING', 'PUBLISHED', 'REJECTED'
          ))
        """
    )
