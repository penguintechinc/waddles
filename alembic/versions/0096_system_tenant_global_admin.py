"""Global identity from tenancy: drop `hub_users.is_super_admin`, rename tenant `global` -> `default`.

Product rule: there is no super-admin flag. The reserved `system` tenant is the operator plane
(security.md Reserved Tenants): a member of `system` is a global user, a tenant admin of `system`
is a global admin (super admin). `default` is the catch-all tenant.

Upgrade (idempotent, in order):

1. `tenant_members` (plain tenant membership) and `tenant_admins` exist (`IF NOT EXISTS`).
2. The catch-all tenant row is renamed `global` -> `default`, KEEPING its id (every FK follows).
   Skipped when `default` already exists and `global` does not; FAILS LOUD when both exist.
3. The reserved `system` tenant exists.
4. Every `is_super_admin = true` user (and the `is_bootstrap` user) becomes a `system` member AND
   a `system` tenant admin (role `tenant-owner`) -- so the bootstrap admin ends up a global admin.
5. `hub_users.is_super_admin` is dropped.

Downgrade restores the column, backfills it from `system` tenant admins and reverses the rename
(only of the `is_global` catch-all row; fails loud if both slugs exist). `tenant_members` and the
`system` tenant row are kept -- dropping them would destroy data the older code merely ignores.

Revision ID: 0096_system_tenant_global_admin
Revises: 0095_bundle_disabled_apps_view
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096_system_tenant_global_admin"
down_revision = "0095_bundle_disabled_apps_view"
branch_labels = None
depends_on = None

SYSTEM_SLUG = "system"
OLD_DEFAULT_SLUG = "global"
NEW_DEFAULT_SLUG = "default"


def _has_column(table: str, column: str) -> bool:
    """True when `table.column` exists in the current schema (minimal test stubs lack many)."""
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
    )


def _tenant_id(slug: str) -> int | None:
    """Id of the tenant with `slug`, or None."""
    row = (
        op.get_bind()
        .execute(sa.text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug})
        .first()
    )
    return int(row[0]) if row else None


def _rename_tenant(old: str, new: str, *, only_global_flag: bool) -> None:
    """Rename tenant `old` -> `new` keeping its id; fail loud if both exist as different rows."""
    old_id = _tenant_id(old)
    new_id = _tenant_id(new)
    if old_id is None:
        return  # nothing to rename (already done, or a deployment that never had `old`)
    if new_id is not None:
        raise RuntimeError(
            f"cannot rename tenant {old!r} (id {old_id}) to {new!r}: a different tenant "
            f"(id {new_id}) already holds that slug; resolve manually"
        )
    if only_global_flag and _has_column("tenants", "is_global"):
        flagged = (
            op.get_bind()
            .execute(sa.text("SELECT is_global FROM tenants WHERE id = :i"), {"i": old_id})
            .scalar()
        )
        if not flagged:
            return
    op.get_bind().execute(
        sa.text("UPDATE tenants SET slug = :new WHERE id = :i"), {"new": new, "i": old_id}
    )


def _ensure_system_tenant() -> int:
    """Create the reserved `system` tenant if missing; return its id."""
    existing = _tenant_id(SYSTEM_SLUG)
    if existing is not None:
        return existing
    cols = ["slug"]
    vals = [":slug"]
    if _has_column("tenants", "display_name"):
        cols.append("display_name")
        vals.append("'System'")
    if _has_column("tenants", "is_global"):
        cols.append("is_global")
        vals.append("FALSE")
    op.get_bind().execute(
        sa.text(f"INSERT INTO tenants ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
        {"slug": SYSTEM_SLUG},
    )
    created = _tenant_id(SYSTEM_SLUG)
    assert created is not None
    return created


def upgrade() -> None:
    """Create membership, rename the catch-all tenant, seed `system`, move admins, drop the flag."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_admins (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
            role VARCHAR(50) NOT NULL DEFAULT 'tenant-admin',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (tenant_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_members (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (tenant_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_tenant_members_user_id ON tenant_members(user_id)")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_members TO hub_api;
                GRANT USAGE, SELECT ON SEQUENCE tenant_members_id_seq TO hub_api;
            END IF;
        END
        $$
        """
    )

    _rename_tenant(OLD_DEFAULT_SLUG, NEW_DEFAULT_SLUG, only_global_flag=False)
    system_id = _ensure_system_tenant()

    if _has_column("hub_users", "is_super_admin"):
        predicate = "is_super_admin = true"
        if _has_column("hub_users", "is_bootstrap"):
            predicate += " OR is_bootstrap = true"
        for table, extra_cols, extra_vals in (
            ("tenant_members", "", ""),
            ("tenant_admins", ", role", ", 'tenant-owner'"),
        ):
            op.execute(
                sa.text(
                    f"INSERT INTO {table} (tenant_id, user_id{extra_cols}) "
                    f"SELECT :sid, id{extra_vals} FROM hub_users WHERE {predicate} "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ).bindparams(sid=system_id)
            )
        op.execute("ALTER TABLE hub_users DROP COLUMN is_super_admin")


def downgrade() -> None:
    """Restore the flag from `system` tenant admins and reverse the catch-all rename."""
    op.execute(
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN DEFAULT FALSE"
    )
    system_id = _tenant_id(SYSTEM_SLUG)
    if system_id is not None:
        op.execute(
            sa.text(
                "UPDATE hub_users SET is_super_admin = true WHERE id IN "
                "(SELECT user_id FROM tenant_admins WHERE tenant_id = :sid)"
            ).bindparams(sid=system_id)
        )
    _rename_tenant(NEW_DEFAULT_SLUG, OLD_DEFAULT_SLUG, only_global_flag=True)
