"""app_core/app_community schemas + waddles_bundle_migrator/waddles_bundle_runtime roles.

Phase 0 of `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md`
(Rev 5) Sec14 Phase 0 row 1: "Create app_core/app_community schemas;
waddles_bundle_migrator (DDL-only, those 2 schemas) and waddles_bundle_runtime
(USAGE+DML, those 2 schemas, ALTER DEFAULT PRIVILEGES, explicit REVOKE ALL
elsewhere) roles." Both are real `LOGIN` roles -- hub-api connects as
`waddles_bundle_migrator` to run bundle-table DDL at approval/upgrade/uninstall
time (Sec2.1), and svc_process/svc_action each open one pool as
`waddles_bundle_runtime` for `tables.*` DML (Sec7) -- so, unlike the
NOLOGIN RBAC-matrix roles `scripts/db/rbac_matrix.py` manages (SET ROLE only,
never a direct connection), both need real passwords. Provisioned the same
way `waddles_bundle_reader` was (migration 0025's own module docstring):
password sourced from the environment (a Kubernetes secret in beta/prod, a
local env var in dev), never hardcoded, bridged into SQL only via a bound
`set_config` parameter -> GUC -> `current_setting()` inside a `DO $$` block
(the exact pattern `0001_baseline_from_sql_migrations.py` already uses for
`INITIAL_ADMIN_PASSWORD`) so the value is never interpolated into SQL text.
An unset/empty password is not a migration failure -- the role is still
created (or left alone) `LOGIN` with whatever password it already has (or
none), matching `DB_READER_PASSWORD`'s graceful-degradation precedent in
`k8s/helm/waddlebot/templates/secrets.yaml`: a not-yet-provisioned credential
disables real traffic through that role, it never blocks the migration.

Every hardening condition from spec Sec2/Sec3.4/Sec7 (Gemini round 3's
C3.1-C3.3, folded into Sec1's history table) is applied here:

- `waddles_bundle_migrator` owns both schemas outright (`CREATE SCHEMA ...
  AUTHORIZATION`) -- DDL rights come from ownership, no separate GRANT needed.
- `waddles_bundle_runtime` gets `USAGE` only on the two app schemas, plus
  `SELECT, INSERT, UPDATE, DELETE` on every table `waddles_bundle_migrator`
  creates in them from now on, via a role-scoped (not schema-wide)
  `ALTER DEFAULT PRIVILEGES FOR ROLE waddles_bundle_migrator` -- a table some
  other role happened to create in `app_core`/`app_community` would NOT
  silently grant runtime DML, matching Sec1's C3.1 resolution verbatim.
- No temp-table creation from the data-plane role, ever. Postgres grants
  `TEMPORARY` on every database to `PUBLIC` by default, and a role's
  effective privilege is the union of its own grants and PUBLIC's, so
  `REVOKE TEMPORARY ... FROM waddles_bundle_runtime` alone is a no-op while
  PUBLIC still holds it (verified empirically) -- both
  `REVOKE TEMPORARY ON DATABASE <current_database()> FROM PUBLIC` and the
  explicit per-role revoke are issued together.
- `waddles_bundle_runtime`'s `search_path` is pinned to
  `app_core, app_community, pg_catalog` (`ALTER ROLE ... SET search_path`) --
  never bundle-influenced, never any control-plane schema.
- Explicit `REVOKE ALL` for `waddles_bundle_runtime` on `public` (the schema
  and everything already in it) -- stated outright rather than relying on "a
  new role starts with nothing", per Sec1's C3.1 "enumerate, don't rely on
  absence-of-grant alone." `public` is the only non-app control-plane schema
  that exists in this database today; a future migration introducing another
  one (Sec2's diagram mentions `billing`/`auth` as illustrative future
  schemas) must add its own explicit REVOKE in that migration.
- Instance-wide baseline (Sec2: "no role, bundle-related or not, gets default
  access to public anymore"): `REVOKE CREATE, USAGE ON SCHEMA public FROM
  PUBLIC`. `CREATE` is already Postgres 15+'s own default (a no-op revoke on
  16/17), but `USAGE` is still PUBLIC-granted by default and every existing
  per-service role (`hub_api`, `svc_ingest`, `svc_process`, `svc_action`,
  `svc_streaming`, `webui`, `migration_runner`, `waddles_publisher`,
  `waddles_bundle_reader`, ...) depends on that implicit grant today --
  revoking it bare would break every one of them. Fixed here, not with a
  hand-maintained role list that would silently drift as new roles/tables
  land, but by introspecting `pg_class`'s own ACLs and ownership in `public`
  at migration time and re-granting `USAGE ON SCHEMA public` explicitly, only
  to roles that actually hold a privilege or ownership there right now. Net
  effect for every pre-existing role: zero behavior change, just an explicit
  grant standing in for what used to be implicit PUBLIC access. Any brand
  new role (including both roles this migration creates) gets nothing it
  wasn't already given.

Revision ID: 0030_bundle_app_schemas
Revises: 0029_bundle_attribution_metadata
Create Date: 2026-09-28
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision = "0030_bundle_app_schemas"
down_revision = "0029_bundle_attribution_metadata"
branch_labels = None
depends_on = None

_MIGRATOR_ROLE = "waddles_bundle_migrator"
_RUNTIME_ROLE = "waddles_bundle_runtime"
_APP_SCHEMAS = ("app_core", "app_community")

#: Every non-app (control-plane) schema this migration knows about at write
#: time -- explicitly REVOKEd from waddles_bundle_runtime below (Sec1 C3.1:
#: enumerate, don't rely on absence-of-grant alone). A fixed snapshot, not a
#: live `pg_namespace` query, so this migration's own behavior stays
#: deterministic run to run; a future control-plane schema needs its own
#: migration adding its own explicit REVOKE, same as this one does for
#: `public`.
_NON_APP_SCHEMAS = ("public",)

_MIGRATOR_PASSWORD_ENV = "BUNDLE_MIGRATOR_PASSWORD"
_RUNTIME_PASSWORD_ENV = "BUNDLE_RUNTIME_PASSWORD"


def _password_guc(role: str) -> str:
    """The `set_config` key a role's password is bridged through -- never the SQL text itself."""
    return f"waddles.{role}_pw"


def _stage_password(conn: Connection, role: str, env_var: str) -> None:
    """Bind `env_var`'s value into a session-local GUC via a parameterized query.

    Mirrors `0001_baseline_from_sql_migrations.py`'s own
    INITIAL_ADMIN_PASSWORD bridge -- the only way a secret value reaches this
    migration's DDL without ever being interpolated into SQL text.
    """
    conn.execute(
        sa.text("SELECT set_config(:key, :v, false)"),
        {"key": _password_guc(role), "v": os.environ.get(env_var, "")},
    )


def _create_or_update_login_role(role: str) -> str:
    """`DO $$` block: create `role` LOGIN if absent, else refresh its password if one was staged.

    An empty staged password means "no credential provided this run" -- the
    role is created/left alone LOGIN with whatever password (or none) it
    already has; never a hard failure (graceful-degradation precedent:
    DB_READER_PASSWORD, k8s/helm/waddlebot/templates/secrets.yaml).
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


#: Every role that currently holds a real privilege or owns an object in
#: `public` gets `USAGE ON SCHEMA public` re-granted explicitly, standing in
#: for the implicit PUBLIC-wide grant this migration revokes below. Reads
#: `pg_class`'s own ACLs (`aclexplode`) and ownership directly via
#: `pg_catalog` rather than `information_schema`, which is visibility-scoped
#: to the executing role and would under-report here.
_PRESERVE_EXISTING_PUBLIC_ACCESS_SQL = """
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT DISTINCT pr.rolname
        FROM pg_class c
        CROSS JOIN LATERAL aclexplode(c.relacl) AS acl
        JOIN pg_roles pr ON pr.oid = acl.grantee
        WHERE c.relnamespace = 'public'::regnamespace
        UNION
        SELECT DISTINCT pr2.rolname
        FROM pg_class c
        JOIN pg_roles pr2 ON pr2.oid = c.relowner
        WHERE c.relnamespace = 'public'::regnamespace
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', r.rolname);
    END LOOP;
END $$;
"""


def upgrade() -> None:
    conn = op.get_bind()

    _stage_password(conn, _MIGRATOR_ROLE, _MIGRATOR_PASSWORD_ENV)
    _stage_password(conn, _RUNTIME_ROLE, _RUNTIME_PASSWORD_ENV)

    op.execute(_create_or_update_login_role(_MIGRATOR_ROLE))
    op.execute(_create_or_update_login_role(_RUNTIME_ROLE))

    # waddles_bundle_migrator OWNS both schemas -- DDL rights (CREATE/ALTER/
    # DROP on tables within) come from ownership, no separate GRANT needed
    # (spec Sec2's role table: "no DML grant needed, it never runs
    # application queries").
    for schema in _APP_SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {_MIGRATOR_ROLE}")
        op.execute(f"COMMENT ON SCHEMA {schema} IS "
                   f"'Bundle-owned data tables (spec Sec2/Sec3) -- DDL via {_MIGRATOR_ROLE} "
                   f"only, DML via {_RUNTIME_ROLE} only'")
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {_RUNTIME_ROLE}")

    # Role-scoped (not schema-wide) default privileges: only tables
    # waddles_bundle_migrator itself creates from now on grant runtime DML
    # (Sec1 C3.1) -- a table some other role created in these schemas would
    # not silently pick up a grant.
    for schema in _APP_SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR_ROLE} IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_RUNTIME_ROLE}"
        )

    # No temp-table creation from the data-plane role, ever. Postgres grants
    # TEMPORARY on every database to PUBLIC by default, and a role's
    # *effective* privilege is the union of its own grants and PUBLIC's --
    # revoking TEMPORARY from waddles_bundle_runtime alone is therefore a
    # no-op in practice as long as PUBLIC still holds it (verified: without
    # this PUBLIC-scoped revoke, `CREATE TEMP TABLE` still succeeds for the
    # role despite the role-scoped REVOKE below). Both revokes together:
    # PUBLIC loses the instance-wide default (no other role here has ever
    # relied on ad hoc temp tables), and the explicit per-role revoke stays
    # for documentation/defense-in-depth even though it's the PUBLIC one
    # doing the actual work. current_database() (not a hardcoded name) so
    # this runs unmodified against any database (dev/CI/beta/prod all use
    # different POSTGRES_DB values).
    op.execute(
        "DO $$ BEGIN "
        "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); "
        f"EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM {_RUNTIME_ROLE}', current_database()); "
        "END $$;"
    )

    # Explicit REVOKE on every known non-app schema -- enumerated, not
    # inferred from "a new role starts with nothing" (Sec1 C3.1).
    for schema in _NON_APP_SCHEMAS:
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {_RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {_RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {_RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {_RUNTIME_ROLE}")

    # Pinned search_path for the data-plane role -- never bundle-influenced,
    # never includes any control-plane schema (spec Sec7).
    op.execute(f"ALTER ROLE {_RUNTIME_ROLE} SET search_path = app_core, app_community, pg_catalog")

    # Instance-wide baseline: no role gets default access to `public`
    # anymore. CREATE is already PG15+'s own default (no-op revoke here);
    # USAGE is still PUBLIC-granted by default, so every role that
    # currently relies on it gets an explicit re-grant first.
    op.execute(_PRESERVE_EXISTING_PUBLIC_ACCESS_SQL)
    op.execute("REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC")


def downgrade() -> None:
    # Reverse of upgrade(), in reverse order. Restoring the pre-migration
    # PUBLIC defaults makes this migration's public-schema hardening step
    # fully reversible too, not just the two new roles/schemas.
    op.execute("GRANT CREATE, USAGE ON SCHEMA public TO PUBLIC")
    op.execute(
        "DO $$ BEGIN "
        "EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC', current_database()); "
        "END $$;"
    )

    op.execute(f"ALTER ROLE {_RUNTIME_ROLE} RESET search_path")

    for schema in _APP_SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR_ROLE} IN SCHEMA {schema} "
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_RUNTIME_ROLE}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {_RUNTIME_ROLE}")
        # RESTRICT (the default), not CASCADE: a non-empty schema here means
        # something unexpected happened after Phase 0 -- fail loudly rather
        # than silently destroying data on downgrade.
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT")

    op.execute(f"DROP ROLE IF EXISTS {_RUNTIME_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_MIGRATOR_ROLE}")
