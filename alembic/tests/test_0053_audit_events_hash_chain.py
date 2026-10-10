"""Tests for 0053_audit_events_hash_chain (the tamper-evident `audit_events` table).

Two layers, same split as the sibling migration tests:

1. Text-level (no database): revision metadata (id fits `alembic_version.version_num`,
   exactly one head, parent exists), and the emitted SQL has the guarantees the migration
   promises -- append-only triggers, the hash-shape CHECKs, `SELECT, INSERT` and *never*
   `UPDATE`/`DELETE` granted.
2. Real Postgres (docker): the migration is run for real and the guarantees are *exercised*:
   UPDATE / DELETE / TRUNCATE are rejected by trigger, forks and duplicate `seq` are rejected
   by constraint, the `hub_api` role really cannot modify history, and upgrade -> downgrade ->
   upgrade round-trips. Skipped, never failed, where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_VERSIONS = Path(__file__).resolve().parent.parent / "versions"
_REVISION = "0053_audit_events_hash_chain"
_GENESIS = "0" * 64


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load(_VERSIONS / f"{_REVISION}.py")


def _captured_sql(fn_name: str, migration: ModuleType) -> list[str]:
    with patch("alembic.op.execute") as mock_execute:
        getattr(migration, fn_name)()
    return [" ".join(str(call.args[0]).split()) for call in mock_execute.call_args_list]


class TestMigrationMetadata:
    def test_revision_id_fits_alembic_version_num_varchar32(
        self, migration: ModuleType
    ) -> None:
        assert migration.revision == _REVISION
        assert len(migration.revision) <= 32

    def test_exactly_one_alembic_head_and_this_revision_is_in_its_ancestry(
        self, migration: ModuleType
    ) -> None:
        """Parallel migrations are landing: the chain must stay linear after any renumber."""
        parents: dict[str, str | None] = {}
        for path in _VERSIONS.glob("*.py"):
            if path.name == "__init__.py":
                continue
            module = _load(path)
            parents[module.revision] = module.down_revision
        assert (
            len(parents) > 40
        )  # a scanner that saw nothing would "prove" a single head
        heads = set(parents) - {p for p in parents.values() if p is not None}
        assert len(heads) == 1, (
            f"expected exactly one alembic head, found {sorted(heads)}"
        )
        assert migration.down_revision in parents, (
            "down_revision names a revision that is missing"
        )
        cursor: str | None = next(iter(heads))
        ancestry: list[str] = []
        while cursor is not None:
            ancestry.append(cursor)
            cursor = parents[cursor]
        assert _REVISION in ancestry
        assert len(ancestry) == len(set(ancestry))  # no cycles

    def test_one_child_per_parent(self, migration: ModuleType) -> None:
        children = [
            _load(p).down_revision
            for p in _VERSIONS.glob("*.py")
            if p.name != "__init__.py"
        ]
        assert children.count(migration.down_revision) == 1


class TestEmittedSql:
    def test_table_has_the_chain_constraints_and_no_foreign_keys(
        self, migration: ModuleType
    ) -> None:
        ddl = _captured_sql("upgrade", migration)[0]
        assert "CREATE TABLE IF NOT EXISTS audit_events" in ddl
        assert (
            "PRIMARY KEY (chain_id, seq)" in ddl
        )  # two writers cannot both commit head+1
        assert "UNIQUE (chain_id, prev_hash)" in ddl  # a chain cannot fork
        assert "UNIQUE (event_id)" in ddl
        assert (
            "prev_hash ~ '^[0-9a-f]{64}$'" in ddl
            and "record_hash ~ '^[0-9a-f]{64}$'" in ddl
        )
        assert (
            "REFERENCES" not in ddl
        )  # erasing a user/tenant must never cascade into history
        assert "actor_uuid UUID" in ddl
        for column in ("ip_address", "user_agent", "username", "email"):
            assert column not in ddl  # no PII columns

    def test_append_only_triggers_and_function_are_created(
        self, migration: ModuleType
    ) -> None:
        sql = "\n".join(_captured_sql("upgrade", migration))
        assert "CREATE OR REPLACE FUNCTION audit_events_reject_mutation()" in sql
        assert "BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW" in sql
        assert "BEFORE TRUNCATE ON audit_events FOR EACH STATEMENT" in sql

    def test_only_select_and_insert_are_ever_granted(
        self, migration: ModuleType
    ) -> None:
        grants = [
            s for s in _captured_sql("upgrade", migration) if s.startswith("GRANT")
        ]
        assert grants == ["GRANT SELECT, INSERT ON audit_events TO hub_api"]
        sql = "\n".join(_captured_sql("upgrade", migration)).upper()
        assert "GRANT UPDATE" not in sql and "GRANT DELETE" not in sql
        assert "DELETE ON AUDIT_EVENTS TO" not in sql

    def test_downgrade_drops_triggers_before_the_table_then_the_function(
        self, migration: ModuleType
    ) -> None:
        statements = _captured_sql("downgrade", migration)
        order = [
            next(i for i, s in enumerate(statements) if needle in s)
            for needle in (
                "DROP TRIGGER IF EXISTS audit_events_no_truncate",
                "DROP TABLE",
                "DROP FUNCTION",
            )
        ]
        assert order == sorted(order)


def _exec(
    db: PgTestDatabase, sql: str, params: object = None
) -> list[tuple[object, ...]]:
    """Run one statement on its own autocommit connection (a failure never poisons the next)."""
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def _row(
    seq: int, *, chain: str = "tenant:1", prev: str | None = None
) -> tuple[object, ...]:
    return (
        chain,
        seq,
        str(uuid.uuid4()),
        "user",
        "admin",
        "admin.action",
        "success",
        prev if prev is not None else (_GENESIS if seq == 1 else f"{seq:064x}"),
        uuid.uuid4().hex + uuid.uuid4().hex,
    )


_INSERT = (
    "INSERT INTO audit_events (chain_id, seq, event_id, occurred_at, actor_kind, category, "
    "action, outcome, prev_hash, record_hash) "
    "VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)"
)


@pytest.fixture(scope="module")
def pg_db() -> PgTestDatabase:  # type: ignore[misc]
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres(f"0048-audit-{os.getpid()}") as db:
        yield db


@requires_docker
class TestRealPostgres:
    def test_migration_creates_the_table_at_head(self, pg_db: PgTestDatabase) -> None:
        rows = _exec(
            pg_db,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'audit_events' ORDER BY ordinal_position",
        )
        columns = [str(r[0]) for r in rows]
        assert columns[:3] == ["chain_id", "seq", "event_id"]
        assert {
            "actor_uuid",
            "prev_hash",
            "record_hash",
            "hash_version",
            "details",
        } <= set(columns)
        assert "ip_address" not in columns and "user_agent" not in columns

    def test_append_is_allowed_and_a_chain_links(self, pg_db: PgTestDatabase) -> None:
        first = _row(1, chain="tenant:append")
        _exec(pg_db, _INSERT, first)
        _exec(pg_db, _INSERT, _row(2, chain="tenant:append", prev=str(first[-1])))
        count = _exec(
            pg_db, "SELECT count(*) FROM audit_events WHERE chain_id = 'tenant:append'"
        )
        assert count == [(2,)]

    @pytest.mark.parametrize("verb", ["UPDATE", "DELETE", "TRUNCATE"])
    def test_update_delete_and_truncate_are_rejected_even_for_the_owner(
        self, pg_db: PgTestDatabase, verb: str
    ) -> None:
        chain = (
            f"tenant:immutable-{verb.lower()}"  # rows are permanent: one chain per case
        )
        statement = {
            "UPDATE": f"UPDATE audit_events SET action = 'admin.rewritten' WHERE chain_id = '{chain}'",
            "DELETE": f"DELETE FROM audit_events WHERE chain_id = '{chain}'",
            "TRUNCATE": "TRUNCATE audit_events",
        }[verb]
        _exec(pg_db, _INSERT, _row(1, chain=chain))
        with pytest.raises(psycopg2.Error, match="append-only"):
            _exec(pg_db, statement)
        survivors = _exec(
            pg_db,
            "SELECT count(*), min(action) FROM audit_events WHERE chain_id = %s",
            (chain,),
        )
        assert survivors == [(1, "admin.action")]

    def test_hub_api_role_has_select_and_insert_only(
        self, pg_db: PgTestDatabase
    ) -> None:
        privileges = {
            privilege: _exec(
                pg_db,
                "SELECT has_table_privilege('hub_api', 'audit_events', %s)",
                (privilege,),
            )[0][0]
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
        assert privileges == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
        }

    def test_a_chain_cannot_fork_and_seq_cannot_repeat(
        self, pg_db: PgTestDatabase
    ) -> None:
        first = _row(1, chain="tenant:fork")
        _exec(pg_db, _INSERT, first)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _exec(pg_db, _INSERT, _row(1, chain="tenant:fork"))  # same (chain, seq)
        _exec(pg_db, _INSERT, _row(2, chain="tenant:fork", prev=str(first[-1])))
        with pytest.raises(
            psycopg2.errors.UniqueViolation
        ):  # a second child of record 1
            _exec(pg_db, _INSERT, _row(3, chain="tenant:fork", prev=str(first[-1])))

    @pytest.mark.parametrize(
        ("index", "bad"),
        [(3, "robot"), (6, "maybe"), (7, "not-hex"), (8, "F" * 64), (8, "a" * 63)],
    )
    def test_vocabulary_and_hash_shape_checks(
        self, pg_db: PgTestDatabase, index: int, bad: str
    ) -> None:
        values = list(_row(1, chain=f"tenant:check-{index}-{len(bad)}"))
        values[index] = bad
        with pytest.raises(psycopg2.errors.CheckViolation):
            _exec(pg_db, _INSERT, values)

    def test_upgrade_downgrade_upgrade_round_trips(self, pg_db: PgTestDatabase) -> None:
        # Step down exactly one revision only if this migration is still the head.
        current = alembic_cli("current", dsn=pg_db.dsn).stdout
        if _REVISION not in current:
            pytest.skip(
                "another migration landed after 0053; the CI chain covers the round trip"
            )
        alembic_cli("downgrade", "-1", dsn=pg_db.dsn)
        assert _exec(pg_db, "SELECT to_regclass('audit_events')") == [(None,)]
        gone = _exec(
            pg_db,
            "SELECT count(*) FROM pg_proc WHERE proname = 'audit_events_reject_mutation'",
        )
        assert gone == [(0,)]
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
        assert _exec(pg_db, "SELECT to_regclass('audit_events')") == [("audit_events",)]
