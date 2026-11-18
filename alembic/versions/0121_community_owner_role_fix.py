"""Repair `community_members.role` rows stored as the non-canonical 'owner'.

`superadmin_service.create_community` used to write role='owner' while every authorization gate
(backend and frontend) checks 'community-owner', so community owners could not reach their admin
panel. The writer is fixed; this migration repairs existing rows: exactly 'owner' becomes
'community-owner', and `community_role_id` is pointed at the community's seeded `community-owner`
role (only for the repaired rows, only where it is still NULL, so a deliberate assignment is never
overwritten) and the cached claims are cleared so they are recomputed.

Idempotent and safe on an empty table or a minimal bootstrap without the tables.
Revision id <= 32 chars; chains off the release head `0118_room_scopes_moderator`.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0121_community_owner_role_fix"
down_revision = "0118_room_scopes_moderator"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    """True if ``public.<name>`` exists (minimal CI bootstraps may lack the legacy tables)."""
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}
        ).scalar()
    )


def _has_column(table: str, column: str) -> bool:
    """True if ``public.<table>.<column>`` exists (claims_cache is absent on a minimal bootstrap)."""
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c)"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    """Rewrite exact 'owner' rows to 'community-owner' and repair the denormalized role id."""
    if not _has_table("community_members"):
        return
    if _has_table("community_roles") and _has_column(
        "community_members", "community_role_id"
    ):
        op.execute(
            "UPDATE community_members cm SET community_role_id = cr.id "
            "FROM community_roles cr "
            "WHERE cm.role = 'owner' AND cm.community_role_id IS NULL "
            "AND cr.community_id = cm.community_id AND cr.name = 'community-owner'"
        )
    clear_cache = (
        ", claims_cache = NULL"
        if _has_column("community_members", "claims_cache")
        else ""
    )
    op.execute(
        f"UPDATE community_members SET role = 'community-owner'{clear_cache} WHERE role = 'owner'"
    )


def downgrade() -> None:
    """No-op: the repair is not reversible without re-breaking owners.

    Rows rewritten here are indistinguishable from rows that were already 'community-owner', and
    restoring the legacy 'owner' value would lock owners out of the admin panel again.
    """
