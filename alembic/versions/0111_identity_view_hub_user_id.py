"""Append `hub_user_id` to the `community_member_identities` data-plane view.

The stage derives a member's per-community call-participant pseudonym
(`uuid5(NS, "<tenant>:<community>:<hub user id>")`, identical to hub-api's
`call_participants.identity_uuid`) so chat moderation and REST address the same
participant while the global `hub_users.uuid` never reaches svc-streaming. The numeric
`hub_users.id` is an opaque integer (no PII); NULL for platform-only members.

Merges the two open heads (0109, 0108 precompile) so this branch has one head. When
#1326's `0110_call_rooms` (which also merges them) lands, re-chain this to
`down_revision = "0110_call_rooms"`.

Revision ID: 0111_identity_view_hub_user_id
Revises: 0109_platform_vault, 0108_bundle_precompile_requests
Create Date: 2026-11-14
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0111_identity_view_hub_user_id"
down_revision = ("0109_platform_vault", "0108_bundle_precompile_requests")
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
