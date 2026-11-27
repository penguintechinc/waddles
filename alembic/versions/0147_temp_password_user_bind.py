"""Bind a temp password to ONE hub user: `hub_temp_passwords.hub_user_id`.

A community admin mints a temp password for an active member of that community; the row now
records that member's hub user id so the login (`auth_service.temp_password_login`) can only
ever authenticate as that very user (and only while they are still an active member). Legacy
unbound rows are refused at login (fail closed). Nullable for those rows; `ON DELETE CASCADE`
removes a bound row with its user (GDPR erasure).

Idempotent: a missing table is a no-op; ADD COLUMN / index / FK use IF [NOT] EXISTS guards.

Revision ID: 0147_temp_password_user_bind
Revises: 0146_membership_state
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0147_temp_password_user_bind"
down_revision = "0146_membership_state"
branch_labels = None
depends_on = None

_FK = "fk_hub_temp_passwords_hub_user_id"
_IDX = "ix_hub_temp_passwords_hub_user_id"


def _has_table(name: str) -> bool:
    """True iff `name` exists (fresh-schema replays may not have it yet)."""
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{name}"}
        ).scalar()
    )


def upgrade() -> None:
    """Add the nullable bound-user column, its index and the cascading FK."""
    if not _has_table("hub_temp_passwords"):
        return
    op.execute("ALTER TABLE hub_temp_passwords ADD COLUMN IF NOT EXISTS hub_user_id INTEGER")
    op.execute(f"CREATE INDEX IF NOT EXISTS {_IDX} ON hub_temp_passwords (hub_user_id)")
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{_FK}') THEN
                ALTER TABLE hub_temp_passwords
                    ADD CONSTRAINT {_FK} FOREIGN KEY (hub_user_id)
                    REFERENCES hub_users(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the FK, index and column."""
    if not _has_table("hub_temp_passwords"):
        return
    op.execute(f"ALTER TABLE hub_temp_passwords DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(f"DROP INDEX IF EXISTS {_IDX}")
    op.execute("ALTER TABLE hub_temp_passwords DROP COLUMN IF EXISTS hub_user_id")
