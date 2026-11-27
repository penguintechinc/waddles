"""Shared real-Postgres harness for the demo-critical hub-api paths (role-sync + event-sync).

Boots one migrated Postgres container (`alembic/tests/pg_docker.py`, the same harness the DSAR /
identity PG suites use), tops up the legacy-table columns the pydal binders declare but the
alembic minimal bootstrap omits, installs `community_roles` + the REAL role-seeding objects, and
hands back a pydal `AsyncDAL` with `bind_auth_tables` + `bind_guild_pairing_tables` bound to it.
"""

from __future__ import annotations

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
from role_seeding import (  # noqa: E402  # type: ignore[import-not-found]
    install_community_role_seeding,
)

from services.schema import bind_auth_tables, bind_guild_pairing_tables  # noqa: E402

__all__ = [
    "DOCKER_AVAILABLE",
    "PgTestDatabase",
    "fresh_dal",
    "migrated_postgres",
    "open_dal",
    "prepare_database",
    "q",
    "scalar",
]

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
    # The real legacy `community_roles` DDL plus the REAL seed function / role-kind triggers
    # (alembic 0002/0117/0118 `upgrade()` bodies), which the minimal bootstrap skipped.
    install_community_role_seeding(db.dsn)
    raw = psycopg2.connect(db.dsn)
    raw.autocommit = True
    with raw.cursor() as cur:
        # entity_public_ids skips on the minimal bootstrap too; add the column it owns here.
        cur.execute(
            "ALTER TABLE community_roles "
            "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
        )
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


def open_dal(db: PgTestDatabase) -> AsyncDAL:
    """A new pydal `AsyncDAL` (own connection) on the container, test tables bound, no truncate."""
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
    bind_guild_pairing_tables(dal, migrate=False)
    return async_dal


def fresh_dal(db: PgTestDatabase) -> Iterator[AsyncDAL]:
    """Generator body for a per-test fixture: truncated tables + bound pydal DAL."""
    async_dal = open_dal(db)
    dal = async_dal.dal
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
