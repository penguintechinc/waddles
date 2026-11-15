"""Append `hub_user_id` to the `community_member_identities` data-plane view.

The stage derives a member's per-community call-participant pseudonym
(`uuid5(NS, "<tenant>:<community>:<hub user id>")`, identical to hub-api's
`call_participants.identity_uuid`) so chat moderation and REST address the same
participant while the global `hub_users.uuid` never reaches svc-streaming. The numeric
`hub_users.id` is an opaque integer (no PII); NULL for platform-only members.

Chained to 0110_merge_precompile_vault for now; re-point down_revision to release's head at
merge time (the single-head gate enforces it).

Revision ID: 0116_identity_view_hub_uid
Revises: 0110_merge_precompile_vault
Create Date: 2026-11-14
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0116_identity_view_hub_uid"
down_revision = "0110_merge_precompile_vault"
branch_labels = None
depends_on = None

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "db"


def upgrade() -> None:
    """Replace the view with the 0070 shape plus a trailing `hub_user_id` column."""
    op.get_bind().exec_driver_sql(
        (_SCRIPTS / "bundle_identity_hub_user_id.sql").read_text("utf-8")
    )


def downgrade() -> None:
    """Drop + recreate the view at its 0070 shape (columns cannot be removed in place)."""
    op.execute("DROP VIEW IF EXISTS community_member_identities")
    op.get_bind().exec_driver_sql(
        (_SCRIPTS / "bundle_identity_role.sql").read_text("utf-8")
    )
