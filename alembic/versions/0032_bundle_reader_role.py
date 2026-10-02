"""waddles_bundle_reader -- the Rust data-plane's real, provisioned RO role.

**Problem this fixes.** `core/svc_process` and `core/svc_action`'s DB-driven
multi-app active-bundle loader (`core/bundle_active_set`) has been fully
wired since migrations 0025/0028 -- both already grant `SELECT` to
`waddles_bundle_reader`, guarded by `IF EXISTS (SELECT 1 FROM pg_roles ...)`
-- but the role itself was never created by any migration. Every one of
those guarded grants has therefore been running as a silent no-op in every
environment, and both services' own startup gate
(`core/svc_process/src/lib.rs::try_start_changelog_consumer`,
`core/svc_action/src/lib.rs` identical) treats an empty `DB_READER_PASSWORD`
as "stay on the legacy single-app env-selected bundle, multi-app path never
starts" -- `values-alpha.yaml`'s own comment says as much ("the RO Postgres
role ... still isn't provisioned yet"). This migration is that missing
piece: it actually creates the role, so alpha (and any other environment)
can run more than one app (`ping` + `pyping`, later `csping`) at once.

**One role, not two.** The Rust/chart default env var value is
`svc_process_ro`/`svc_action_ro` (`core/svc_process/src/config.rs`,
`core/svc_action/src/config.rs` -- `DB_READER_USER` CLI/env default), but
that name was never backed by an actual migration either, and the two
migrations that DO grant real privileges (0025, 0028) both target
`waddles_bundle_reader`. Standardizing on `waddles_bundle_reader` (the name
three existing migrations already reference) rather than inventing a third
name or reviving `svc_process_ro`/`svc_action_ro` -- the Helm chart's
`DB_READER_USER` default is updated to match in the same change that adds
this migration, so both services connect as the role this migration (and
0025/0028's pre-existing grants) actually provisions.

**Password bridge -- same GUC pattern as 0001/0030, same secret key as the
services.** `DB_READER_PASSWORD` already exists end-to-end (Helm renders it
into the `waddlebot-secrets` Secret, both services already read it) -- this
migration reads the SAME env var (via the db-migrate initContainer, which
now also receives it) rather than inventing a parallel
`BUNDLE_READER_PASSWORD`, so the migration that creates the role and the
services that log in as it are never fed two independently-managed secret
values that could drift apart. Bridged into SQL only via a bound
`set_config` parameter -> GUC -> `current_setting()` inside a `DO $$` block
(`0001_baseline_from_sql_migrations.py`'s own `INITIAL_ADMIN_PASSWORD`
pattern, mirrored again by 0030 for `waddles_bundle_migrator`/
`waddles_bundle_runtime`) -- the password is never interpolated into SQL
text, and this migration never logs it. An unset/empty value is not a
migration failure: the role is created (or left alone) `LOGIN` with
whatever password (or none) it already has, identical to 0030's own
graceful-degradation precedent -- a not-yet-provisioned credential disables
real traffic through the role, it never blocks the migration.

**Grants -- exactly the six base tables `core/bundle_active_set`'s own
crate-root doc enumerates (`app_active_versions`, `app_versions`,
`app_install_approvals`, `app_source_bindings`, `tenants`, `communities`),
plus the two change-log tables 0028 introduced
(`bundle_active_set_changes`, `bundle_active_set_watermark`) -- nothing
else.** `SELECT` only, no table this role doesn't actually read. Each grant
is guarded by `to_regclass()` the same way 0025/0028 already guard their own
(now-finally-live) grants to this role, so this migration stays a safe
no-op against a table that doesn't exist yet in some unusual migration
ordering, rather than a hard failure.

**`USAGE ON SCHEMA public` -- required because of 0030's own hardening.**
0030 revoked the implicit `PUBLIC`-wide `USAGE ON SCHEMA public` grant and
re-granted it explicitly only to roles that already held a privilege or
owned an object in `public` *at that migration's own run time* --
`waddles_bundle_reader` did not exist yet, so it never received that
re-grant. Every table this role reads lives in `public` (none of them are
in the new `app_core`/`app_community` schemas 0030 introduced), so without
an explicit `GRANT USAGE ON SCHEMA public` here, every one of the `SELECT`
grants below would be unreachable -- `SELECT` privilege on a table doesn't
help a role that lacks `USAGE` on the schema it lives in.

Revision ID: 0032_bundle_reader_role
Revises: 0031_upload_status_changed_at
Create Date: 2026-10-02
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision = "0032_bundle_reader_role"
down_revision = "0031_upload_status_changed_at"
branch_labels = None
depends_on = None

_READER_ROLE = "waddles_bundle_reader"
_READER_PASSWORD_ENV = "DB_READER_PASSWORD"

#: Exactly what `core/bundle_active_set`'s crate-root doc + migrations
#: 0025/0028 already enumerate as this role's reads -- nothing else.
_READER_TABLES = (
    "app_active_versions",
    "app_versions",
    "app_install_approvals",
    "app_source_bindings",
    "tenants",
    "communities",
    "bundle_active_set_changes",
    "bundle_active_set_watermark",
)


def _password_guc(role: str) -> str:
    """The `set_config` key a role's password is bridged through -- never the SQL text itself."""
    return f"waddles.{role}_pw"


def _stage_password(conn: Connection, role: str, env_var: str) -> None:
    """Bind `env_var`'s value into a session-local GUC via a parameterized query.

    Mirrors `0001_baseline_from_sql_migrations.py`'s INITIAL_ADMIN_PASSWORD
    bridge and `0030_bundle_app_schemas.py`'s identical helper for
    waddles_bundle_migrator/waddles_bundle_runtime -- the only way a secret
    value reaches this migration's DDL without ever being interpolated into
    SQL text.
    """
    conn.execute(
        sa.text("SELECT set_config(:key, :v, false)"),
        {"key": _password_guc(role), "v": os.environ.get(env_var, "")},
    )


def _create_or_update_login_role(role: str) -> str:
    """`DO $$` block: create `role` LOGIN if absent, else refresh its password if one was staged.

    An empty staged password means "no credential provided this run" -- the
    role is created/left alone LOGIN with whatever password (or none) it
    already has; never a hard failure (same graceful-degradation precedent
    as 0030 / DB_READER_PASSWORD's existing consumers).
    """
    guc = _password_guc(role)
    return f"""
DO $$
DECLARE
    pw text := NULLIF(current_setting('{guc}', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        IF pw IS NULL THEN
            CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSE
            EXECUTE format(
                'CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
                pw
            );
        END IF;
    ELSIF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE {role} PASSWORD %L', pw);
    END IF;
END $$;
"""


def _grant_sql(table: str) -> str:
    """Guarded `GRANT SELECT ON {table} TO waddles_bundle_reader` -- table must exist."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF to_regclass('{table}') IS NOT NULL THEN\n"
        f"    GRANT SELECT ON {table} TO {_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def _revoke_sql(table: str) -> str:
    """Guarded `REVOKE SELECT ON {table} FROM waddles_bundle_reader` -- the `upgrade()` grant's inverse."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF to_regclass('{table}') IS NOT NULL THEN\n"
        f"    REVOKE SELECT ON {table} FROM {_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def upgrade() -> None:
    conn = op.get_bind()

    _stage_password(conn, _READER_ROLE, _READER_PASSWORD_ENV)
    op.execute(_create_or_update_login_role(_READER_ROLE))

    # Required by 0030's own public-schema hardening: USAGE ON SCHEMA public
    # is no longer PUBLIC-wide, and this role did not exist when 0030 ran its
    # one-time "preserve existing access" re-grant. Every table this role
    # reads lives in `public`, so SELECT below is unreachable without this.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_READER_ROLE}")

    for table in _READER_TABLES:
        op.execute(_grant_sql(table))


def downgrade() -> None:
    for table in reversed(_READER_TABLES):
        op.execute(_revoke_sql(table))

    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {_READER_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_READER_ROLE}")
