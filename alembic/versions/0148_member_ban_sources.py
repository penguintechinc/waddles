"""Record WHY a membership is banned: `community_members.ban_sources` (bitmask).

`membership_state` (0146) says a row is BANNED but not why, so lifting one reason (a soft-ban
clear, a floor-ban lift, a suppression lift) un-banned a member still banned for another --
fail open. This adds `ban_sources SMALLINT NOT NULL DEFAULT 0`, a
`services/membership_state.py::BanSource` bitmask (1 manual, 2 soft ban, 4 floor ban,
8 suppression). Every ban path ORs its bit in, every lift clears only its own bit, and the row
is BANNED while any bit remains (`services/ban_suppression.py`).

Backfill (fail closed): every existing BANNED row with no recorded source gets MANUAL, so only
an explicit manual lift can restore it. CHECKs bound the column to the defined bits and forbid
a source on a non-BANNED row.

Idempotent: a missing table is a no-op; ADD COLUMN / CHECK use IF [NOT] EXISTS guards.

Revision ID: 0148_member_ban_sources
Revises: 0147_temp_password_user_bind
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0148_member_ban_sources"
down_revision = "0147_temp_password_user_bind"
branch_labels = None
depends_on = None

_RANGE = "ck_community_members_ban_sources"
_BANNED = "ck_community_members_ban_sources_banned"
_MANUAL = 1
_ALL = 15


def _has_table(name: str) -> bool:
    """True iff `name` exists (fresh-schema replays may not have it yet)."""
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{name}"}
        ).scalar()
    )


def _add_check(name: str, expr: str) -> None:
    """Add CHECK ``name`` on `community_members` unless it already exists."""
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
                ALTER TABLE community_members ADD CONSTRAINT {name} CHECK ({expr});
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    """Add the ban-source bitmask, backfill BANNED rows as MANUAL, add its CHECKs."""
    if not _has_table("community_members"):
        return
    op.execute(
        "ALTER TABLE community_members "
        "ADD COLUMN IF NOT EXISTS ban_sources SMALLINT NOT NULL DEFAULT 0"
    )
    op.execute(
        f"UPDATE community_members SET ban_sources = {_MANUAL} "
        "WHERE membership_state = 2 AND ban_sources = 0"
    )
    _add_check(_RANGE, f"ban_sources BETWEEN 0 AND {_ALL}")
    _add_check(_BANNED, "ban_sources = 0 OR membership_state = 2")


def downgrade() -> None:
    """Drop the CHECKs and the column (the BANNED state itself is kept)."""
    if not _has_table("community_members"):
        return
    op.execute(f"ALTER TABLE community_members DROP CONSTRAINT IF EXISTS {_BANNED}")
    op.execute(f"ALTER TABLE community_members DROP CONSTRAINT IF EXISTS {_RANGE}")
    op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS ban_sources")
