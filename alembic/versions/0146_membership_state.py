"""Ban becomes a membership STATE: `community_members.membership_state` (1 active, 2 banned).

Community authority lives in `community_roles` (scopes on the role, via `community_role_id`); the
legacy `community_members.role = 'banned'` string was the last place a ban hid inside a role name.
This adds the int-enum standing column `community_members.membership_state SMALLINT`
(`services/membership_state.py::MembershipState`: 1 active, 2 banned), backfills every legacy
`role = 'banned'` row to BANNED, and guards the closed set with a CHECK. A lift writes 1 back.
`role` itself is NOT dropped here (separate, later revision once nothing reads it).

Idempotent: a missing table is a no-op; ADD COLUMN / CHECK use IF [NOT] EXISTS guards.

Revision ID: 0146_membership_state
Revises: 0145_community_role_backfill
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0146_membership_state"
down_revision = "0145_community_role_backfill"
branch_labels = None
depends_on = None

_CHECK = "ck_community_members_membership_state"


def _has_table(name: str) -> bool:
    """True iff `name` exists (fresh-schema replays may not have it yet)."""
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{name}"}
        ).scalar()
    )


def _upgrade_members() -> None:
    """Add the state column, backfill legacy 'banned' role rows, add the closed-set CHECK."""
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


def upgrade() -> None:
    """Add the membership state column (backfilled) and its closed-set CHECK."""
    if _has_table("community_members"):
        _upgrade_members()


def _downgrade_members() -> None:
    """Fold BANNED back into the legacy role string, then drop the CHECK and the column."""
    op.execute(
        "UPDATE community_members SET role = 'banned' WHERE membership_state = 2"
    )
    op.execute(f"ALTER TABLE community_members DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS membership_state")


def downgrade() -> None:
    """Fold BANNED back into the legacy role string and drop the column."""
    if _has_table("community_members"):
        _downgrade_members()
