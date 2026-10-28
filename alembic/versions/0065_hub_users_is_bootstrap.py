"""Explicit bootstrap-admin marker on ``hub_users`` (free-tier admin cap).

The free-tier global-admin cap excludes the seeded initial super admin (migration 081) from its
count. Identifying that admin as ``min(id)`` among super admins was fragile: delete or demote it
and the next-lowest super admin silently became "bootstrap", freeing a slot. This adds an
explicit, stable marker instead.

- ``is_bootstrap BOOLEAN NOT NULL DEFAULT false`` -- true on exactly the seeded initial admin.
- Backfill: the lowest-id super admin existing at upgrade time (the 081-seeded row; this runs
  before any user-created admin on a deployment that is just receiving the cap). No super admin
  at upgrade time -> nothing is marked (the cap then counts every super admin).
- Partial unique index ``ux_hub_users_single_bootstrap`` -- at most one bootstrap row, ever.

Revision ID: 0065_hub_users_is_bootstrap
Revises: 0064_connection_kinds
Create Date: 2026-10-25
"""

from __future__ import annotations

from alembic import op

revision = "0065_hub_users_is_bootstrap"
down_revision = "0064_connection_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the marker column, backfill the seeded admin, enforce single-row uniqueness."""
    op.execute(
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_bootstrap BOOLEAN NOT NULL DEFAULT false"
    )
    # Guarded on the column: minimal hub_users stubs (test bootstraps) lack is_super_admin.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'hub_users' AND column_name = 'is_super_admin'
            ) THEN
                UPDATE hub_users SET is_bootstrap = true
                WHERE id = (SELECT min(id) FROM hub_users WHERE is_super_admin = true)
                  AND NOT EXISTS (SELECT 1 FROM hub_users WHERE is_bootstrap = true);
            END IF;
        END
        $$
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_hub_users_single_bootstrap "
        "ON hub_users ((true)) WHERE is_bootstrap"
    )


def downgrade() -> None:
    """Drop the index and the column (the marker is derived data; nothing else depends on it)."""
    op.execute("DROP INDEX IF EXISTS ux_hub_users_single_bootstrap")
    op.execute("ALTER TABLE hub_users DROP COLUMN IF EXISTS is_bootstrap")
