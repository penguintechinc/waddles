"""Bundle identity resolution read contract (stage-next `identity` host capability).

The points-game bundles (`!gamble`, `!steal @user`, `!duel @user`) must name the
triggering actor -- and a mentioned target -- by the community `user_uuid` the
`economy`/`reputation` host capabilities require. A bundle only ever holds the
tokenized `{user:<uuid>}` placeholder, which is NOT guaranteed to equal
`community_members.user_uuid`, so the stage (not the bundle) resolves it:
`core/svc_process::identity` reads `community_member_identities` -- the existing
PII-free data-plane view (0043/0045) -- under the read-only `waddles_bundle_reader`
role it already uses for the grant tables.

This migration only widens that view, append-only (`CREATE OR REPLACE VIEW` may
add columns, never reorder/drop): `tenant_id` (every lookup is tenant-scoped) and
`is_active_member` (the same active-membership predicate economy/reputation
apply, fail-closed on a NULL `is_active`). No new role, password, table or
privilege, and no PII column is projected -- the migration tests re-assert that.

The DDL lives in `scripts/db/bundle_identity_resolve.sql` (copied into the
migrations image, and `include_str!`'d by the Rust end-to-end test) so the
shipped schema and the tested schema cannot drift.

Revision ID: 0048_bundle_identity_resolve
Revises: 0047_bundle_economy_store
Create Date: 2026-10-09
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0048_bundle_identity_resolve"
down_revision = "0047_bundle_economy_store"
branch_labels = None
depends_on = None

_READER = "waddles_bundle_reader"
_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_identity_resolve.sql"
)

#: The 0045 view shape, restored verbatim by `downgrade()`. A view cannot DROP
#: columns via CREATE OR REPLACE, so the downgrade drops and recreates it.
_VIEW_0045 = (
    "CREATE VIEW community_member_identities AS "
    "SELECT cm.community_id, cm.platform, cm.platform_user_id, hu.uuid AS hub_user_uuid, "
    "cm.user_uuid "
    "FROM community_members cm LEFT JOIN hub_users hu ON hu.id::text = cm.user_id"
)


def _grant_reader() -> str:
    return (
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_READER}') THEN "
        f"GRANT SELECT ON community_member_identities TO {_READER}; END IF; END $$;"
    )


def upgrade() -> None:
    """Append `tenant_id` + `is_active_member` to the data-plane identity view."""
    conn = op.get_bind()
    # exec_driver_sql: the file is a multi-statement script with `$$` blocks and
    # quotes; bypass SQLAlchemy `text()` bind-parameter parsing entirely.
    conn.exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Restore the 0045 view shape (drop + recreate; columns cannot be removed in place)."""
    op.execute("DROP VIEW IF EXISTS community_member_identities")
    op.execute(_VIEW_0045)
    op.execute(_grant_reader())
