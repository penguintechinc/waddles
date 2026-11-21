"""`0137_discord_guild_tenants` -- one tenant per Discord guild, DB-enforced.

Mocked-`op` assertions (no DB) plus a real-Postgres round trip that proves the two promises the
migration makes about EXISTING data: a backfill that seeds one claim per paired guild, and a loud,
state-preserving failure when a guild is already paired across two tenants.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0137_discord_guild_tenants.py"
)
_HUB_API_ROOT = Path(__file__).resolve().parents[2] / "hub_api"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0137_dgt", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_chains_off_0136(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0137_discord_guild_tenants"
        assert migration.down_revision == "0136_app_id_owners"
        assert len(migration.revision) <= 32

    def test_guild_id_is_the_primary_key_so_a_claim_is_atomic(self) -> None:
        sql = _load_migration()._CREATE_SQL
        assert "discord_guild_id VARCHAR(255) PRIMARY KEY" in sql
        assert "tenant_id        INTEGER      NOT NULL REFERENCES tenants(id)" in sql
        assert "claimed_by_uuid  UUID" in sql

    def test_columns_match_the_pydal_binding(self) -> None:
        if str(_HUB_API_ROOT) not in sys.path:
            sys.path.insert(0, str(_HUB_API_ROOT))
        from pydal import DAL
        from services.schema import (
            bind_guild_pairing_tables,  # type: ignore[import-not-found]
        )

        dal = DAL("sqlite:memory")
        dal.define_table("tenants", dal.Field("slug"))
        bind_guild_pairing_tables(dal, migrate=True)
        assert set(dal.discord_guild_tenants.fields) == {
            "discord_guild_id",
            "tenant_id",
            "claimed_at",
            "claimed_by_uuid",
        }


class TestUpgradeRefusesCrossTenantData:
    def test_upgrade_raises_loudly_before_touching_the_schema(self) -> None:
        migration = _load_migration()
        bind = MagicMock()
        bind.execute.return_value = [("111", [1, 2])]
        with (
            patch.object(migration.op, "get_bind", return_value=bind),
            patch.object(migration.op, "execute") as execute,
            pytest.raises(RuntimeError, match="more than one tenant") as err,
        ):
            migration.upgrade()
        assert "guild 111 in tenants [1, 2]" in str(err.value)
        execute.assert_not_called()

    def test_clean_data_creates_backfills_and_grants(self) -> None:
        migration = _load_migration()
        bind = MagicMock()
        bind.execute.return_value = []
        with (
            patch.object(migration.op, "get_bind", return_value=bind),
            patch.object(migration.op, "execute") as execute,
        ):
            migration.upgrade()
        sql = "\n".join(str(c.args[0]) for c in execute.call_args_list)
        assert "CREATE TABLE IF NOT EXISTS discord_guild_tenants" in sql
        assert "INSERT INTO discord_guild_tenants" in sql
        assert "GRANT" in sql and "discord_guild_tenants" in sql
        assert sql.index("CREATE TABLE") < sql.index(
            "INSERT INTO discord_guild_tenants"
        )


@pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)
def test_real_postgres_backfill_and_loud_failure() -> None:
    """On a real chain: backfill seeds claims; a cross-tenant guild aborts the migration."""
    import psycopg2

    def run(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall()) if cur.description else []

    with migrated_postgres("0137-guild-tenants") as db:
        run(db, "SELECT 1")
        alembic_cli("downgrade", "0136_app_id_owners", dsn=db.dsn)
        assert run(db, "SELECT to_regclass('discord_guild_tenants')") == [(None,)]

        tenants = [
            run(
                db,
                "INSERT INTO tenants (slug) VALUES (%s) RETURNING id",
                (slug,),
            )[0][0]
            for slug in ("alpha-corp", "beta-corp")
        ]
        comms = [
            run(
                db,
                "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
                (f"c{i}", t),
            )[0][0]
            for i, t in enumerate(tenants)
        ]
        pair = (
            "INSERT INTO guild_tenant_pairings "
            "(community_id, discord_guild_id, direction, role_name_prefix) "
            "VALUES (%s, %s, 'bidirectional', '[T]')"
        )
        run(db, pair, (comms[0], "111"))
        run(db, pair, (comms[1], "111"))  # the SAME guild in two tenants
        run(db, pair, (comms[1], "222"))

        with pytest.raises(RuntimeError, match="more than one tenant"):
            alembic_cli("upgrade", "head", dsn=db.dsn)
        assert run(db, "SELECT to_regclass('discord_guild_tenants')") == [(None,)], (
            "a refused migration must leave no table behind"
        )

        run(
            db,
            "DELETE FROM guild_tenant_pairings WHERE community_id = %s AND discord_guild_id = '111'",
            (comms[1],),
        )
        alembic_cli("upgrade", "head", dsn=db.dsn)
        claims = dict(
            run(db, "SELECT discord_guild_id, tenant_id FROM discord_guild_tenants")
        )
        assert claims == {"111": tenants[0], "222": tenants[1]}
