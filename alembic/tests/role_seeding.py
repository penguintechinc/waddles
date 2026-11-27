"""Install the REAL community-role seeding objects on a minimal-bootstrap test Postgres.

The minimal CI bootstrap (`pg_docker.migrated_postgres`) has no `community_roles`, so alembic 0002 /
0117 / 0118 skip and `seed_community_system_roles()` plus its role-kind triggers never exist. This
creates the table from the legacy 058 DDL and then runs those three revisions' own `upgrade()`
bodies -- no copy of any role name or scope list lives in test code.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _ROOT / "alembic" / "versions"
_LEGACY_058 = (
    _ROOT / "config" / "postgres" / "migrations" / "058_tenants_and_claims.sql"
)

#: The revisions that define the seed function and the BEFORE INSERT role triggers, in order.
SEEDING_REVISIONS = (
    "0002_channel_creation_policy.py",
    "0117_community_role_kind.py",
    "0118_room_scopes_moderator.py",
)


def community_roles_ddl() -> str:
    """The real `community_roles` CREATE TABLE out of legacy `058_tenants_and_claims.sql`."""
    sql = _LEGACY_058.read_text()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS community_roles \(.*?\n\);", sql, re.DOTALL
    )
    assert match is not None, "community_roles DDL moved in 058 -- update role_seeding"
    return match.group(0)


def _upgrade(conn: sa.Connection, filename: str) -> None:
    """Run one revision file's `upgrade()` against `conn` through alembic's `op` proxy."""
    spec = importlib.util.spec_from_file_location(
        f"seed_{filename[:4]}", _VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(conn)):
        module.upgrade()


def install_community_role_seeding(dsn: str) -> None:
    """Create `community_roles` (+ the member link columns) and install the real seeding objects."""
    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(community_roles_ddl())
            conn.exec_driver_sql(
                "ALTER TABLE community_members "
                "ADD COLUMN IF NOT EXISTS community_role_id INTEGER, "
                "ADD COLUMN IF NOT EXISTS claims_cache JSONB"
            )
            for filename in SEEDING_REVISIONS:
                _upgrade(conn, filename)
    finally:
        engine.dispose()
