"""Ban becomes a membership STATE: `community_members.membership_state SMALLINT` (1 active, 2 banned).

Community authority lives in `community_roles` (scopes on the role, via `community_role_id`); the
legacy `community_members.role = 'banned'` string was the last place a ban hid inside a role name.
This adds the int-enum standing column (`services/membership_state.py::MembershipState`), backfills
every legacy `role = 'banned'` row to BANNED, and guards the closed set with a CHECK. A lift writes
1 back. `role` itself is NOT dropped here (separate, later revision once nothing reads it).

Idempotent: a missing table is a no-op; ADD COLUMN / CHECK use IF NOT EXISTS guards.

Revision ID: 0145_membership_state
Revises: 0144_ingest_owner_public_ids
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0145_membership_state"
down_revision = "0144_ingest_owner_public_ids"
branch_labels = None
depends_on = None

_CHECK = "ck_community_members_membership_state"


def _has_members_table() -> bool:
    """True iff `community_members` exists (fresh-schema replays may not have it yet)."""
    conn = op.get_bind()
    return bool(
        conn.execute(sa.text("SELECT to_regclass('public.community_members') IS NOT NULL")).scalar()
    )


def upgrade() -> None:
    """Add the state column, backfill legacy 'banned' role rows, add the closed-set CHECK."""
    if not _has_members_table():
        return
    op.execute(
        "ALTER TABLE community_members "
        "ADD COLUMN IF NOT EXISTS membership_state SMALLINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "UPDATE community_members SET membership_state = 2 "
        "WHERE lower(trim(role)) = 'banned' AND membership_state <> 2"
    )
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{_CHECK}') THEN
                ALTER TABLE community_members
                    ADD CONSTRAINT {_CHECK} CHECK (membership_state IN (1, 2));
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Fold BANNED back into the legacy role string, then drop the CHECK and the column."""
    if not _has_members_table():
        return
    op.execute("UPDATE community_members SET role = 'banned' WHERE membership_state = 2")
    op.execute(f"ALTER TABLE community_members DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS membership_state")
