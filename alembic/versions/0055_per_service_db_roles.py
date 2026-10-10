"""Per-service least-privilege LOGIN roles; neutralize repo-known-credential roles.

Fixes security findings **H-1** and **H-3**.

**H-3 -- one shared DB superuser.** Every workload the chart deploys connected as the
database owner/superuser (`waddlebot`): a compromise of ANY pod (or any `envFrom` of the
shared Secret) was a full database -- and, via `COPY ... PROGRAM`, DB-host -- takeover.
This migration provisions one non-superuser LOGIN role per workload from
`config/postgres/service-roles.yaml` (via `scripts/db/service_roles.py`, the single place
that writes these GRANTs). Strict roles get an explicit table list (e.g.
`waddles_svc_action` can INSERT into `action_dispatch_log` and nothing else of the
v3 schema); the control-plane role gets DML but no DDL; each legacy pod gets its own
credential. The owner credential is confined to the Postgres Deployment and the
db-migrate Job by the chart.

**H-1 -- repo-known passwords.** `031_scoped_database_users.sql` and
`config/postgres/init.sql` shipped `*_dev_changeme` passwords for ~35 LOGIN roles
including `hub_admin` (ALL PRIVILEGES, CREATE on schema public, EXECUTE on the
SECURITY DEFINER `provision_module_db_account` whose `custom_grants` runs arbitrary SQL
as the owner -- i.e. repo password => superuser, reproduced against a real fresh replay
before this fix). 031 no longer contains any password; this migration strips LOGIN and the
password from every such role on EXISTING databases, and revokes the SECURITY DEFINER
escalation functions from `hub_admin`. Treat the old values as burned: they are in git
history forever (see docs/DATABASE_CREDENTIALS.md for rotation).

**Local/dev opt-out.** With `WADDLES_DEV_DB_ROLE_PW_SUFFIX` set (docker-compose's
`db-migrations` only; refused when `WADDLES_DEPLOYMENT_TIER` is alpha/beta/gamma/
production -- see `alembic/env.py`) the legacy roles are left LOGIN-able and service-role
passwords default to `<role><suffix>`. Nothing in the chart sets it.

**Credentials.** `WADDLES_DB_SERVICE_ROLE_PASSWORDS` (JSON role -> password) is REQUIRED;
a missing/short/placeholder password fails the migration loudly rather than provisioning a
role with no usable credential. It is bound through a `set_config` GUC, never
interpolated into SQL text or logged.

**Idempotent / fresh-replay safe.** Missing tables or groups (the minimal CI chain only
bootstraps six legacy tables) are skipped, not errors; `migrations/run-alembic.sh` then
runs `service_roles.py reconcile --strict` after `alembic upgrade head`, which fails loud
on genuine catalog drift and re-asserts the catalog on every migrate Job run (rotation,
tables added by later migrations).

**Downgrade** drops the service roles (privileges revoked via `DROP OWNED BY`). It does
NOT restore the repo-known passwords -- re-enabling them would re-open H-1.

Revision ID: 0055_per_service_db_roles
Revises: 0049_sso_connections
Create Date: 2026-10-10
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0055_per_service_db_roles"
down_revision = "0049_sso_connections"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")
_SERVICE_ROLES_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "service_roles.py"


def _service_roles() -> ModuleType:
    """Import scripts/db/service_roles.py by path (alembic/ is a sibling of scripts/)."""
    spec = importlib.util.spec_from_file_location("waddles_service_roles_0055", _SERVICE_ROLES_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_SERVICE_ROLES_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    roles = _service_roles()
    conn = op.get_bind()
    catalog = roles.load_catalog()
    dev_mode = bool(roles.dev_suffix_from_env())
    passwords = roles.resolve_passwords(catalog)

    report = roles.reconcile(conn, catalog, passwords, strict=False)
    _LOG.info(
        "0055: provisioned %d service roles (%d table grants; skipped groups on %d roles; "
        "catalog tables absent on %d roles)",
        len(report.roles_applied),
        sum(report.granted_table_count.values()),
        len(report.skipped_groups),
        len(report.missing_tables),
    )
    if dev_mode:
        _LOG.warning(
            "0055: dev mode -- repo-credential legacy roles left LOGIN-able (local/dev only)"
        )
        return
    neutralized = roles.neutralize_repo_credential_roles(conn)
    _LOG.info("0055: neutralized %d repo-credential legacy roles", len(neutralized))


def downgrade() -> None:
    roles = _service_roles()
    catalog = roles.load_catalog()
    for name in reversed(catalog.names):
        ident = roles.quote_ident(name)
        op.execute(
            f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
        EXECUTE 'DROP OWNED BY {ident}';
        EXECUTE 'DROP ROLE {ident}';
    END IF;
END $$;
"""  # noqa: S608  # nosec B608 -- name is a catalog-validated identifier, never user input
        )
