"""Data-defined role protections: `community_roles.role_kind` int code.

Predefined-role protections (admin/moderator cannot be deleted, admin scopes cannot be edited,
moderator scopes can) used to hang on role NAMES. They now hang on this int column so authorization
and role management never branch on a name:

    0 custom      -- full CRUD
    1 admin       -- undeletable; scopes are COMPUTED as every registered scope (never stored)
    2 moderator   -- undeletable; scopes editable by an admin holding `roles:manage`
    3 fixed       -- other predefined roles (member, communicator, speaker): undeletable, scopes locked

The backfill keys on the seeded system-role names ONCE, here, in data; a BEFORE INSERT trigger does
the same for every future `seed_community_system_roles()` call, so new communities need no code.
`community-owner` shares the admin kind (full scope set). Revision id <= 32 chars; `down_revision`
is re-pointed to the release alembic head at merge (single-head CI gate).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0117_community_role_kind"
down_revision = "0109_platform_vault"
branch_labels = None
depends_on = None

_KIND_CASE = (
    "CASE name WHEN 'community-admin' THEN 1 WHEN 'community-owner' THEN 1 "
    "WHEN 'moderator' THEN 2 ELSE 3 END"
)


def upgrade() -> None:
    """Add the column, backfill predefined rows, and install the seeding trigger."""
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "ALTER TABLE community_roles "
            "ADD COLUMN IF NOT EXISTS role_kind SMALLINT NOT NULL DEFAULT 0"
        )
    )
    conn.execute(
        sa.text(f"UPDATE community_roles SET role_kind = {_KIND_CASE} WHERE is_system = TRUE")
    )
    conn.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION community_roles_set_kind() RETURNS trigger AS $$
            BEGIN
              IF NEW.is_system AND COALESCE(NEW.role_kind, 0) = 0 THEN
                NEW.role_kind := CASE NEW.name WHEN 'community-admin' THEN 1
                  WHEN 'community-owner' THEN 1 WHEN 'moderator' THEN 2 ELSE 3 END;
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_community_roles_kind ON community_roles"))
    conn.execute(
        sa.text(
            "CREATE TRIGGER trg_community_roles_kind BEFORE INSERT ON community_roles "
            "FOR EACH ROW EXECUTE FUNCTION community_roles_set_kind()"
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE community_roles ADD CONSTRAINT ck_community_roles_kind "
            "CHECK (role_kind IN (0, 1, 2, 3))"
        )
    )


def downgrade() -> None:
    """Drop the trigger, constraint and column."""
    conn = op.get_bind()
    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_community_roles_kind ON community_roles"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS community_roles_set_kind()"))
    conn.execute(
        sa.text("ALTER TABLE community_roles DROP CONSTRAINT IF EXISTS ck_community_roles_kind")
    )
    conn.execute(sa.text("ALTER TABLE community_roles DROP COLUMN IF EXISTS role_kind"))
