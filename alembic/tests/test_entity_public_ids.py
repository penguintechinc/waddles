"""Real-Postgres tests for the entity `public_id` migration (`*_entity_public_ids.py`).

The migration adds `public_id UUID NOT NULL DEFAULT gen_random_uuid()` + a unique index to every
entity table whose route still takes a numeric id. Runs `alembic upgrade`/`downgrade` against a
throwaway Postgres 17 (the `pg_docker` harness) from its parent revision, with minimal stand-ins
for the legacy tables (the 96-file SQL baseline is not replayed, same convention as the rest of
this directory), and proves: every listed table ends with a unique non-null `public_id`, rows that
existed before the upgrade are backfilled with distinct UUIDs, downgrade removes exactly the
column/index and keeps the rows, the upgrade is idempotent, and a table absent from the database
is skipped instead of failing the chain.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, empty_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_VERSIONS = Path(__file__).resolve().parent.parent / "versions"
_MATCHES = sorted(_VERSIONS.glob("*_entity_public_ids.py"))
ROWS_PER_TABLE = 3
#: A listed table the "absent table" test drops before upgrading.
_ABSENT = "user_passkeys"
#: Tables that already had `public_id` before this migration; it must not touch them.
_UNTOUCHED = ("communities", "tenants")


def _load() -> ModuleType:
    assert len(_MATCHES) == 1, (
        f"expected exactly one entity_public_ids migration, got {_MATCHES}"
    )
    spec = importlib.util.spec_from_file_location(
        "entity_public_ids_migration", _MATCHES[0]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load()
TABLES: tuple[str, ...] = tuple(MIGRATION.ENTITY_TABLES)
REVISION: str = MIGRATION.revision
PRIOR: str = MIGRATION.down_revision


def _query(
    db: PgTestDatabase, sql: str, args: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall()) if cur.description else []
    finally:
        conn.close()


def _columns(db: PgTestDatabase, table: str) -> dict[str, tuple[str, str]]:
    rows = _query(
        db,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = %s",
        (table,),
    )
    return {name: (dtype, nullable) for name, dtype, nullable in rows}


def _unique_public_id_index(db: PgTestDatabase, table: str) -> bool:
    rows = _query(
        db,
        "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
        (table, f"uq_{table}_public_id"),
    )
    return bool(rows) and "UNIQUE" in rows[0][0] and "(public_id)" in rows[0][0]


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    """One Postgres at the parent revision: stand-in tables with pre-existing rows, stamped."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with empty_postgres("entity-public-ids") as db:
        ddl = [
            f"CREATE TABLE {t} (id BIGSERIAL PRIMARY KEY, name TEXT)" for t in TABLES
        ] + [
            f"INSERT INTO {t} (name) SELECT 'row-' || g FROM generate_series(1, {ROWS_PER_TABLE}) g"
            for t in TABLES
        ]
        ddl += [
            f"CREATE TABLE {t} (id SERIAL PRIMARY KEY, public_id UUID NOT NULL DEFAULT "
            f"gen_random_uuid() UNIQUE)"
            for t in _UNTOUCHED
        ]
        _query(db, ";\n".join(ddl))
        alembic_cli("stamp", PRIOR, dsn=db.dsn)
        yield db


def _heads(db: PgTestDatabase) -> list[str]:
    return [r[0] for r in _query(db, "SELECT version_num FROM alembic_version")]


def test_migration_chain_has_single_head_after_this_revision() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_VERSIONS.parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(_VERSIONS.parent))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"alembic heads must be 1, got {heads}"
    assert len(TABLES) == len(set(TABLES)) == 26


@requires_docker
class TestRealPostgres:
    """Ordered (module-scoped container): upgrade -> invariants -> downgrade -> re-upgrade."""

    def test_01_upgrade_adds_unique_non_null_backfilled_public_id(
        self, pg: PgTestDatabase
    ) -> None:
        for table in TABLES:
            assert "public_id" not in _columns(pg, table)
        alembic_cli("upgrade", REVISION, dsn=pg.dsn)
        assert _heads(pg) == [REVISION]
        for table in TABLES:
            dtype, nullable = _columns(pg, table)["public_id"]
            assert (dtype, nullable) == ("uuid", "NO"), table
            assert _unique_public_id_index(pg, table), table
            rows = _query(pg, f"SELECT public_id FROM {table}")
            assert len(rows) == ROWS_PER_TABLE, table
            assert all(r[0] is not None for r in rows), table
            assert len({r[0] for r in rows}) == ROWS_PER_TABLE, (
                table
            )  # backfilled, distinct

    def test_02_default_applies_to_new_rows_and_uniqueness_is_enforced(
        self, pg: PgTestDatabase
    ) -> None:
        for table in TABLES:
            _query(pg, f"INSERT INTO {table} (name) VALUES ('fresh')")
            rows = _query(pg, f"SELECT public_id FROM {table}")
            assert len({r[0] for r in rows}) == ROWS_PER_TABLE + 1, table
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _query(
                    pg,
                    f"INSERT INTO {table} (name, public_id) "
                    f"SELECT 'dup', public_id FROM {table} LIMIT 1",
                )
            with pytest.raises(psycopg2.errors.NotNullViolation):
                _query(
                    pg, f"INSERT INTO {table} (name, public_id) VALUES ('null', NULL)"
                )

    def test_03_untouched_tables_keep_their_own_public_id(
        self, pg: PgTestDatabase
    ) -> None:
        for table in _UNTOUCHED:
            assert (
                _unique_public_id_index(pg, table) is False
            )  # no uq_<t>_public_id added
            assert "public_id" in _columns(pg, table)

    def test_04_downgrade_removes_exactly_the_column_and_keeps_rows(
        self, pg: PgTestDatabase
    ) -> None:
        alembic_cli("downgrade", PRIOR, dsn=pg.dsn)
        assert _heads(pg) == [PRIOR]
        for table in TABLES:
            assert "public_id" not in _columns(pg, table), table
            assert not _unique_public_id_index(pg, table), table
            count = _query(pg, f"SELECT count(*) FROM {table}")[0][0]
            assert count == ROWS_PER_TABLE + 1, table
        for table in _UNTOUCHED:
            assert "public_id" in _columns(pg, table)

    def test_05_reupgrade_backfills_rows_that_arrived_while_downgraded(
        self, pg: PgTestDatabase
    ) -> None:
        alembic_cli("upgrade", REVISION, dsn=pg.dsn)
        for table in TABLES:
            rows = _query(pg, f"SELECT public_id FROM {table}")
            assert len(rows) == ROWS_PER_TABLE + 1, table
            assert len({r[0] for r in rows}) == len(rows), table
            assert all(r[0] is not None for r in rows), table

    def test_06_upgrade_is_idempotent_over_a_half_applied_state(
        self, pg: PgTestDatabase
    ) -> None:
        # Re-run only the upgrade body against the already-migrated schema: nothing may change.
        before = {
            t: _query(pg, f"SELECT id, public_id FROM {t} ORDER BY id") for t in TABLES
        }
        alembic_cli("stamp", PRIOR, dsn=pg.dsn)
        alembic_cli("upgrade", REVISION, dsn=pg.dsn)
        after = {
            t: _query(pg, f"SELECT id, public_id FROM {t} ORDER BY id") for t in TABLES
        }
        assert before == after

    def test_07_absent_table_is_skipped_not_fatal(self, pg: PgTestDatabase) -> None:
        alembic_cli("downgrade", PRIOR, dsn=pg.dsn)
        _query(pg, f"DROP TABLE {_ABSENT}")
        alembic_cli("upgrade", REVISION, dsn=pg.dsn)
        assert _heads(pg) == [REVISION]
        for table in TABLES:
            if table == _ABSENT:
                assert _columns(pg, table) == {}
            else:
                assert "public_id" in _columns(pg, table), table
        alembic_cli(
            "downgrade", PRIOR, dsn=pg.dsn
        )  # downgrade also tolerates the absent table
        assert _heads(pg) == [PRIOR]

    def test_08_unknown_absent_table_fails_loudly(self, pg: PgTestDatabase) -> None:
        # `guild_tenant_pairings` is created by migration 0034 on every chain -- it is NOT a
        # known minimal-bootstrap absentee, so its absence must abort the migration.
        victim = "guild_tenant_pairings"
        assert victim not in MIGRATION.MINIMAL_BOOTSTRAP_ABSENT
        _query(pg, f"ALTER TABLE {victim} RENAME TO {victim}_gone")
        try:
            with pytest.raises(RuntimeError, match=victim):
                alembic_cli("upgrade", REVISION, dsn=pg.dsn)
            assert _heads(pg) == [
                PRIOR
            ]  # the failed upgrade did not advance the revision
        finally:
            _query(pg, f"ALTER TABLE {victim}_gone RENAME TO {victim}")


def test_allowlist_is_a_subset_of_the_listed_tables() -> None:
    assert MIGRATION.MINIMAL_BOOTSTRAP_ABSENT <= set(TABLES)
    # tables created by alembic 0020+ must never be allowlisted (their absence is a bug)
    for created_by_alembic in (
        "guild_tenant_pairings",
        "community_role_sync_bindings",
        "discord_role_mirror_mappings",
    ):
        assert created_by_alembic not in MIGRATION.MINIMAL_BOOTSTRAP_ABSENT


def test_typoed_table_name_raises_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    bind = MagicMock()
    bind.exec_driver_sql.return_value = SimpleNamespace(
        scalar=lambda: None
    )  # to_regclass -> NULL
    monkeypatch.setattr(MIGRATION.op, "get_bind", lambda: bind)
    monkeypatch.setattr(MIGRATION.op, "execute", MagicMock())
    monkeypatch.setattr(MIGRATION, "ENTITY_TABLES", {"announcmentz": "announcement_id"})
    with pytest.raises(RuntimeError, match="announcmentz"):
        MIGRATION.upgrade()
    with pytest.raises(RuntimeError, match="announcmentz"):
        MIGRATION.downgrade()
    MIGRATION.op.execute.assert_not_called()
