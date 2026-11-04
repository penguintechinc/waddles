"""Grant `one_time_secrets` to the `public_dml` per-service roles.

0066 created the table after 0055 provisioned the per-service roles, and (unlike the
deploy-time `service_roles.py reconcile --strict`) a plain `alembic upgrade head` never
re-granted it, so every `public_dml` role lacked SELECT/INSERT/UPDATE/DELETE on it and
`test_0055_per_service_db_roles` failed with `missing=[('one_time_secrets', ...)]`.
0066 is not edited (deployed DBs never replay it); this revision grants idempotently,
driven by the same catalog 0055 uses, only to roles that exist and are `public_dml`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0072_one_time_secrets_grants"
down_revision = "0070_identity_view_role"
branch_labels = None
depends_on = None

_TABLE = "one_time_secrets"
_SERVICE_ROLES_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "service_roles.py"


def _service_roles() -> ModuleType:
    """Import scripts/db/service_roles.py by path (alembic/ is a sibling of scripts/)."""
    spec = importlib.util.spec_from_file_location("waddles_service_roles_0072", _SERVICE_ROLES_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_SERVICE_ROLES_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _public_dml_roles(roles: ModuleType) -> list[str]:
    """Quoted names of catalog `public_dml` roles that exist and are not denied the table."""
    catalog = roles.load_catalog()
    conn = op.get_bind()
    out: list[str] = []
    for spec in catalog.roles:
        if not spec.public_dml or _TABLE in spec.deny_tables:
            continue
        if spec.is_legacy and _TABLE in (catalog.legacy_deny_tables - spec.allow_tables):
            continue
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (spec.name,)
        ).scalar()
        if exists:
            out.append(roles.quote_ident(spec.name))
    return out


def upgrade() -> None:
    """GRANT full DML on the table to each existing `public_dml` role (idempotent)."""
    for role in _public_dml_roles(_service_roles()):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {role}")


def downgrade() -> None:
    """REVOKE the grants again; the table itself stays (it belongs to 0066)."""
    for role in _public_dml_roles(_service_roles()):
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {_TABLE} FROM {role}")
