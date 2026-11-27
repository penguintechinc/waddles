"""Drop the legacy authority columns: `community_members.role`, `communities.owner_id/owner_name`.

Community authority lives ONLY in `community_roles` (scopes on the role), linked per member by
`community_members.community_role_id`; the owner is the active member linked to the system
`community-owner` role. Nothing reads or writes the legacy columns any more, so this revision:

1. Links any member still unlinked from its legacy `role` string (the 0145 backfill, re-run so a
   row written between 0145 and this drop never loses its role). Unresolvable rows stay NULL (no
   scopes, fail closed) and are counted.
2. Re-points the data-plane contracts at the linked role (`scripts/db/community_role_link_contract
   .sql`, shared with the Rust tests): the `community_member_identities` view keeps its columns,
   `role` becoming the linked role's name; `waddles_platform_table` is granted the link columns.
3. Drops `idx_community_members_role`, the legacy `community_members_role_check` (if any) and
   the columns. `communities.owner_id` / `owner_name` were free-form, never-verified values (the
   old superadmin "reassign" wrote them without granting anything), so they are NOT promoted into
   ownership -- they are display/legacy only and are dropped as-is.

Minimal CI bootstraps without `community_roles` get the real 058 `community_roles` DDL and the two
member link columns first (the same guarded-prerequisite pattern as `bundle_identity_role.sql`),
so the contract view can be built and the drop runs on every schema.

Downgrade re-adds the columns NULLABLE and repopulates them from the linked roles: `role` = the
linked role's name, `owner_id` = the owner member's `user_id`, `owner_name` = that hub user's
username (display only); the view/grants return to their 0116/0079 shape (the link grants are
revoked) and a minimal-bootstrap `community_roles` this revision created (marked by a table
comment) is removed again, so older revisions' downgrades see the schema they expect.

Revision id <= 32 chars; `down_revision` is #1542's head (re-pointed at merge -- single-head gate).

Revision ID: 0148_retire_member_role
Revises: 0147_temp_password_user_bind
Create Date: 2026-11-27
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa

from alembic import op

revision = "0148_retire_member_role"
down_revision = "0147_temp_password_user_bind"
branch_labels = None
depends_on = None

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts" / "db"
_LEGACY_058 = _ROOT / "config" / "postgres" / "migrations" / "058_tenants_and_claims.sql"
_ROLE_INDEX = "idx_community_members_role"
_ROLE_CHECK = "community_members_role_check"
#: Marks a `community_roles` table THIS revision created on a minimal bootstrap, so the downgrade
#: removes exactly what it added (a real legacy table never carries it).
_STUB_MARK = "0148: minimal-bootstrap community_roles (dropped by its downgrade)"


def _exists(sql: str, **params: str) -> bool:
    """Run a boolean catalog probe."""
    return bool(op.get_bind().execute(sa.text(sql), params).scalar())


def _has_table(name: str) -> bool:
    """True iff ``public.<name>`` exists."""
    return _exists("SELECT to_regclass(:t) IS NOT NULL", t=f"public.{name}")


def _has_column(table: str, column: str) -> bool:
    """True iff ``public.<table>.<column>`` exists."""
    return _exists(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = :t AND column_name = :c)",
        t=table,
        c=column,
    )


def _run_script(name: str) -> None:
    """Execute one shared `scripts/db/*.sql` file verbatim."""
    op.get_bind().exec_driver_sql((_SCRIPTS / name).read_text("utf-8"))


def _backfill_module() -> ModuleType:
    """The 0145 revision module: its `backfill_links` is THE legacy-string -> link backfill."""
    path = Path(__file__).with_name("0145_community_role_backfill.py")
    spec = importlib.util.spec_from_file_location("_rev_0145_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_link_prerequisites() -> None:
    """Minimal bootstraps: the real 058 `community_roles` DDL + the member link columns."""
    if not _has_table("community_roles"):
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS community_roles \(.*?\n\);",
            _LEGACY_058.read_text("utf-8"),
            re.DOTALL,
        )
        if match is None:
            raise RuntimeError("community_roles DDL moved out of 058_tenants_and_claims.sql")
        op.execute(match.group(0))
        op.execute(f"COMMENT ON TABLE community_roles IS '{_STUB_MARK}'")
    op.execute(
        "ALTER TABLE community_members "
        "ADD COLUMN IF NOT EXISTS community_role_id INTEGER REFERENCES community_roles(id), "
        "ADD COLUMN IF NOT EXISTS claims_cache JSONB DEFAULT NULL"
    )


def _upgrade_members() -> None:
    """Final link backfill, contract swap, then drop the legacy `role` column."""
    _ensure_link_prerequisites()
    if _has_column("community_members", "role"):
        linked, batches = _backfill_module().backfill_links(op.get_bind())
        unresolved = op.get_bind().execute(
            sa.text(
                "SELECT count(*) FROM community_members "
                "WHERE community_role_id IS NULL AND role IS NOT NULL"
            )
        ).scalar()
        print(
            f"[0148] linked {linked} members in {batches} batches; {unresolved} members name "
            "no role row (no scopes) and lose only their dead role string"
        )
    if _has_table("hub_users"):
        _run_script("community_role_link_contract.sql")
    op.execute(f"DROP INDEX IF EXISTS {_ROLE_INDEX}")
    op.execute(f"ALTER TABLE community_members DROP CONSTRAINT IF EXISTS {_ROLE_CHECK}")
    op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS role")


def upgrade() -> None:
    """Retire `community_members.role` and `communities.owner_id/owner_name`."""
    if _has_table("community_members"):
        _upgrade_members()
    if _has_table("communities"):
        op.execute("ALTER TABLE communities DROP COLUMN IF EXISTS owner_id")
        op.execute("ALTER TABLE communities DROP COLUMN IF EXISTS owner_name")


_REPOPULATE_ROLE = """
    UPDATE community_members m
       SET role = r.name
      FROM community_roles r
     WHERE r.id = m.community_role_id AND m.role IS NULL
"""

#: The owner = first active member linked to the community's system owner role (lowest id, the
#: same tie-break as hub-api's `community_authz.community_owner_member`).
_REPOPULATE_OWNER = """
    UPDATE communities c
       SET owner_id = o.user_id, owner_name = o.username
      FROM (
        SELECT DISTINCT ON (m.community_id) m.community_id, m.user_id, hu.username
          FROM community_members m
          JOIN community_roles r ON r.id = m.community_role_id
          LEFT JOIN hub_users hu ON hu.id::text = m.user_id
         WHERE r.is_system AND r.name = 'community-owner' AND m.is_active IS TRUE
         ORDER BY m.community_id, m.id
      ) o
     WHERE o.community_id = c.id
"""


_REVOKE_LINK_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_platform_table') THEN
        REVOKE SELECT (community_role_id), UPDATE (community_role_id, claims_cache)
            ON community_members FROM waddles_platform_table;
        REVOKE SELECT (id, community_id, name, is_system) ON community_roles
            FROM waddles_platform_table;
    END IF;
END
$$;
"""


def _drop_minimal_stub() -> None:
    """Remove the minimal-bootstrap prerequisites this revision created (marker-gated)."""
    stub = _exists(
        "SELECT obj_description('public.community_roles'::regclass, 'pg_class') = :m",
        m=_STUB_MARK,
    )
    if stub:
        op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS community_role_id")
        op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS claims_cache")
        op.execute("DROP TABLE community_roles")


def downgrade() -> None:
    """Re-add the columns nullable, repopulated from the linked roles; restore the old contracts."""
    if _has_table("communities"):
        op.execute(
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS owner_id VARCHAR(255), "
            "ADD COLUMN IF NOT EXISTS owner_name VARCHAR(255)"
        )
    if not _has_table("community_members"):
        return
    op.execute("ALTER TABLE community_members ADD COLUMN IF NOT EXISTS role VARCHAR(50)")
    linked = _has_table("community_roles") and _has_column("community_members", "community_role_id")
    if linked:
        op.execute(_REPOPULATE_ROLE)
        if _has_table("communities") and _has_column("hub_users", "username"):
            op.execute(_REPOPULATE_OWNER)
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_ROLE_INDEX} ON community_members (community_id, role)"
    )
    if _has_table("hub_users"):
        # 0116 shape (`role` = the string column again); CREATE OR REPLACE keeps the columns.
        _run_script("bundle_identity_hub_user_id.sql")
    if _has_table("bundle_platform_table_audit"):
        _run_script("bundle_platform_table.sql")
    if linked:
        op.execute(_REVOKE_LINK_GRANTS)
        _drop_minimal_stub()
