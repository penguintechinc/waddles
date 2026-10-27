"""app_version_uploads.status_changed_at -- a real-FSM-transition-only timestamp.

Fixes the alpha 2026-10-01 incident: `create_version()`'s stall check
(`services/bundle_version_service.py`) used `updated_at` to decide whether a
non-terminal `app_version_uploads` row (e.g. stuck in `ADDRESSING`) had been
abandoned by a dead uploader. `updated_at` is also bumped by writes that do
NOT change `status` -- `_set_staging_component_key()` and
`_publish_prebuilt_version()`'s `app_version_id` write both touch it as a
side effect -- so a row that kept making (ultimately incomplete) progress on
every core-bundle-seeder run looked "fresh" forever and the stall check never
fired, 409ing on every retry.

`status_changed_at` is written ONLY by `advance_state()`, the sole function
that ever changes `status` (plus the two other genuine status transitions in
`create_version()` itself: the stall-abandon REJECT and the REJECTED-row
reuse reset to `UPLOADED`) -- a column with exactly one semantic writer
instead of patching every non-FSM write site one at a time (fragile against
future additions that touch the row without a status change).

Backfilled from `updated_at` (falling back to `created_at`) for existing
rows -- a reasonable one-time approximation; going forward only a real
status transition moves it.

Revision ID: 0031_upload_status_changed_at
Revises: 0030_bundle_app_schemas
Create Date: 2026-10-01
"""

from __future__ import annotations

from alembic import op

revision = "0031_upload_status_changed_at"
down_revision = "0030_bundle_app_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add `status_changed_at`, backfilled from `updated_at`/`created_at`."""
    op.execute(
        "ALTER TABLE app_version_uploads "
        "ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE app_version_uploads "
        "SET status_changed_at = COALESCE(updated_at, created_at) "
        "WHERE status_changed_at IS NULL"
    )
    op.execute(
        "ALTER TABLE app_version_uploads "
        "ALTER COLUMN status_changed_at SET NOT NULL, "
        "ALTER COLUMN status_changed_at SET DEFAULT NOW()"
    )
    op.execute(
        "COMMENT ON COLUMN app_version_uploads.status_changed_at IS "
        "'Set only by a real FSM transition (advance_state() and create_version()''s own "
        "stall-abandon/REJECTED-reuse writes) -- never by a same-status column write. "
        "create_version()''s stall check reads this, not updated_at, so it cannot be "
        "defeated by a row that keeps touching updated_at without actually progressing'"
    )


def downgrade() -> None:
    """Drop `status_changed_at` -- inverse of `upgrade()`."""
    op.execute("ALTER TABLE app_version_uploads DROP COLUMN IF EXISTS status_changed_at")
