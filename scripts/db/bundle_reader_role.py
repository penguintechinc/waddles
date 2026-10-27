"""Shared `waddles_bundle_reader` role/grant definition.

fix/no-empty-kept-secrets -- the single source of truth for BOTH
`alembic/versions/0032_bundle_reader_role.py` (one-time provisioning, via the
same `importlib.util.spec_from_file_location` pattern `scripts/db/rbac_matrix.py`'s
other callers already use -- see e.g. `0020_ingest_sources_rbac.py`) AND
`migrations/run-alembic.sh`'s post-`alembic upgrade head` reconcile step (runs on
EVERY migration-job invocation, not just the one that first creates the role).

**Why reconcile on every run, not just once at creation.** A one-time `CREATE ROLE
... PASSWORD` is insufficient: the chart's `waddlebot.autoSecretValue` lookup-KEEP
policy can mint a brand-new `DB_READER_PASSWORD` on a later `helm upgrade` (e.g.
once an existing EMPTY kept value is correctly treated as missing -- the chart-side
half of this fix) without ever touching the role's actual Postgres password, which
would otherwise drift silently out of sync with the Secret forever. Both call sites
run through this exact same SQL so the grant list can never have two
independently-maintained copies that drift apart from each other.

**Password handling.** The value is bound through a session-local `set_config` GUC
and read back inside a `DO $$` block via `format('%L', ...)` -- never interpolated
into SQL text directly, never logged, never passed as a CLI argument (so it never
appears in `ps`/argv).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection

#: The real, provisioned RO role for the Rust data-plane's DB-driven multi-app
#: active-bundle loader (`core/bundle_active_set`). See 0032's own module
#: docstring for why this name, not `svc_process_ro`/`svc_action_ro`.
ROLE = "waddles_bundle_reader"

#: Env var both the migration Job and the reconcile step below read -- the exact
#: same key the chart renders into `waddlebot-secrets` / the db-migrate hook Secret.
PASSWORD_ENV = "DB_READER_PASSWORD"

#: Exactly what `core/bundle_active_set`'s crate-root doc enumerates as this role's
#: reads -- nothing else. The single list both call sites grant from.
TABLES = (
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


def reconcile_role_and_grants(conn: Connection, password: str) -> None:
    """Idempotently (re)create `ROLE` LOGIN with `password`, grant schema USAGE + SELECT on TABLES.

    Safe to call repeatedly against an already-provisioned role (ALTER ROLE
    branch) or a not-yet-existing one (CREATE ROLE branch) -- used both by
    0032's one-time `upgrade()` and by `migrations/run-alembic.sh`'s
    every-run reconcile step.

    Raises ValueError if `password` is empty -- callers MUST fail loud before
    calling this (see 0032's `upgrade()` guard and run-alembic.sh's own `[ -z
    ... ]` check) so an empty env var can never silently leave/set an empty,
    unusable password on this role (the exact bug class this fix addresses:
    an empty password here disables the DB-driven multi-app path with no
    visible error).
    """
    if not password:
        raise ValueError(
            f"{PASSWORD_ENV} is empty -- refusing to reconcile {ROLE} with no "
            "usable password (this role gates the DB-driven multi-app "
            "active-bundle path; an empty-password role would silently leave "
            "it disabled)"
        )

    guc = _password_guc(ROLE)
    conn.execute(sa.text("SELECT set_config(:key, :v, false)"), {"key": guc, "v": password})
    conn.execute(
        sa.text(
            f"""
DO $$
DECLARE
    pw text := current_setting('{guc}', true);
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
        EXECUTE format(
            'CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
            pw
        );
    ELSE
        EXECUTE format('ALTER ROLE {ROLE} WITH LOGIN PASSWORD %L', pw);
    END IF;
    GRANT USAGE ON SCHEMA public TO {ROLE};
END $$;
"""
        )
    )
    for table in TABLES:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals from TABLES above, never user input
                f"  IF to_regclass('{table}') IS NOT NULL THEN\n"
                f"    GRANT SELECT ON {table} TO {ROLE};\n"
                f"  END IF;\n"
                f"END $$;"
            )
        )
