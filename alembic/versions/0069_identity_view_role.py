"""Append `role` to the `community_member_identities` data-plane view.

The stage resolves a tool caller's community role through this PII-free view
(`MemberDirectory::role_for`) and maps it to a fixed host-side scope set so
admin-scoped bundle tools are enforced HOST-SIDE. `role` is an opaque closed enum
(community-owner/-admin, moderator, vip, member) -- no PII. Reuses the existing
read-only `waddles_bundle_reader` grant (re-asserted by the SQL file).

Revision ID: 0069_identity_view_role
Revises: 0068_app_catalog_kind
Create Date: 2026-10-30
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0069_identity_view_role"
down_revision = "0068_app_catalog_kind"
branch_labels = None
depends_on = None

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "db"


def upgrade() -> None:
    """Replace the view with the 0052 shape plus a trailing `role` column."""
    op.get_bind().exec_driver_sql(
        (_SCRIPTS / "bundle_identity_role.sql").read_text("utf-8")
    )


def downgrade() -> None:
    """Drop + recreate the view at its 0052 shape (columns cannot be removed in place)."""
    op.execute("DROP VIEW IF EXISTS community_member_identities")
    op.get_bind().exec_driver_sql(
        (_SCRIPTS / "bundle_identity_resolve.sql").read_text("utf-8")
    )
