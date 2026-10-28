"""Shared real-Postgres harness for the demo-critical hub-api paths (role-sync + event-sync).

Boots one migrated Postgres container (`alembic/tests/pg_docker.py`, the same harness the DSAR /
identity PG suites use), tops up the legacy-table columns the pydal binders declare but the
alembic minimal bootstrap omits, creates `community_roles` from the REAL legacy DDL, and hands
back a pydal `AsyncDAL` with `bind_auth_tables` + `bind_bar_citizen_tables` bound against it.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
from flask_core.database import AsyncDAL
from pydal import Field

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.schema import bind_auth_tables, bind_bar_citizen_tables  # noqa: E402

__all__ = [
    "DOCKER_AVAILABLE",
    "PgTestDatabase",
    "fresh_dal",
    "migrated_postgres",
    "prepare_database",
    "q",
    "scalar",
]

LEGACY_SQL = Path(__file__).resolve().parents[2] / "config" / "postgres" / "migrations"

_PG_TYPES = {
    "string": "VARCHAR(255)",
    "text": "TEXT",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "datetime": "TIMESTAMP",
    "json": "JSONB",
}

#: Legacy tables whose real DDL is wider than alembic's minimal bootstrap.
_TOPUP_TABLES = ("communities", "hub_users", "hub_user_identities", "community_members", "tenants")


def _community_roles_ddl() -> str:
    """The REAL `community_roles` DDL out of legacy `058_tenants_and_claims.sql` (no copy)."""
    sql = (LEGACY_SQL / "058_tenants_and_claims.sql").read_text()
    match = re.search(r"CREATE TABLE IF NOT EXISTS community_roles \(.*?\n\);", sql, re.DOTALL)
    assert match is not None, "community_roles DDL moved in 058 -- update demo_pg_support"
    return match.group(0)


def prepare_database(db: PgTestDatabase) -> None:
    """Add every pydal-declared column missing from the minimal legacy bootstrap tables."""
    scratch = AsyncDAL(db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    dal = scratch.dal
    dal.define_table(
        "tenants",
        Field("slug"),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean"),
        Field("is_active", "boolean"),
        Field("config", "json"),
        migrate=False,
    )
    bind_auth_tables(dal, migrate=False)
    raw = psycopg2.connect(db.dsn)
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute(_community_roles_ddl())
        for table in _TOPUP_TABLES:
            for field in dal[table]:
                if field.name == "id":
                    continue
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {field.name} "  # noqa: S608
                    f"{_PG_TYPES[field.type.split('(')[0].split(' ')[0]]}"
                )
    raw.close()
    dal.close()


def fresh_dal(db: PgTestDatabase) -> Iterator[AsyncDAL]:
    """Generator body for a per-test fixture: truncated tables + bound pydal DAL."""
    async_dal = AsyncDAL(db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug"),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean"),
        Field("is_active", "boolean"),
        Field("config", "json"),
        migrate=False,
    )
    bind_auth_tables(dal, migrate=False)
    bind_bar_citizen_tables(dal, migrate=False)
    dal.executesql(
        "TRUNCATE guild_tenant_pairings, community_role_sync_bindings, community_roles, "
        "community_members, hub_user_identities, hub_users, communities, tenants "
        "RESTART IDENTITY CASCADE"
    )
    dal.commit()
    yield async_dal
    dal.close()


def q(dal: Any, sql: str, params: list[Any] | None = None) -> list[Any]:
    """Run raw SQL through the DAL's own connection and commit; rows or `[]`."""
    rows = dal.executesql(sql, placeholders=params or [])
    dal.commit()
    return list(rows or [])


def scalar(dal: Any, sql: str, params: list[Any] | None = None) -> Any:
    """First column of the first row, `None` when no rows."""
    rows = q(dal, sql, params)
    return rows[0][0] if rows else None
