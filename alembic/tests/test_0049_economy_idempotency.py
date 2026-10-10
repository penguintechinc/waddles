"""Real-Postgres tests for 0049_economy_idempotency (#751 money-safety review).

Runs the actual Alembic chain to `head` in an ephemeral container (see
`pg_docker.py`) and asserts the idempotency column, its shape CHECK, the partial
UNIQUE index that is the database's backstop against crediting one key twice,
that the runtime role can still only APPEND to the ledger, and that the DDL is
re-appliable -- none of which a mocked `op.execute` could verify.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "versions" / "0049_economy_idempotency.py"
)
_ROLE = "waddles_economy_runtime"
_USER = "11111111-2222-4333-8444-555555555555"
_KEY = "3fa85f64-5717-4562-b3fc-2c963f66afa6:wager:0"


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0049", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_chains_off_0048_identity_resolve(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0049_economy_idempotency"
        assert migration.down_revision == "0048_bundle_identity_resolve"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32

    def test_ddl_file_the_migration_loads_exists(self) -> None:
        assert _load_migration()._SQL_PATH.is_file()


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0049-economy-idempotency") as db:
        yield db


@contextmanager
def _cursor(db: PgTestDatabase) -> Iterator[Any]:
    """One AUTOCOMMIT cursor (never `with conn`): a raised constraint error must not poison the next statement."""
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        dbname=db.dbname,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def _scope(cur: Any) -> tuple[int, int]:
    """A fresh (tenant, community) pair to key ledger rows under."""
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"idem-{uuid.uuid4()}",)
    )
    tid = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (f"idem-c-{uuid.uuid4()}", tid),
    )
    return tid, cur.fetchone()[0]


def _insert_wager(cur: Any, tid: int, cid: int, app: str, key: str | None) -> None:
    cur.execute(
        "INSERT INTO economy_ledger "
        "(tenant_id, community_id, app_id, user_uuid, kind, delta, stake, payout, "
        " balance_after, idempotency_key) "
        "VALUES (%s, %s, %s, %s, 'wager', 9, 1, 10, 10, %s)",
        (tid, cid, app, _USER, key),
    )


@requires_docker
class TestSchema:
    def test_ledger_has_a_nullable_idempotency_key_varchar_128(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'economy_ledger' AND column_name = 'idempotency_key'"
            )
            assert cur.fetchone() == ("character varying", 128, "YES")

    def test_the_unique_index_is_partial_and_scoped_to_tenant_community_app(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_economy_ledger_idempotency'"
            )
            row = cur.fetchone()
        assert row is not None
        definition = row[0]
        assert definition.startswith("CREATE UNIQUE INDEX")
        assert "(tenant_id, community_id, app_id, idempotency_key)" in definition
        assert "idempotency_key IS NOT NULL" in definition

    def test_a_key_is_claimed_once_per_tenant_community_app(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            _insert_wager(cur, tid, cid, "app.a", _KEY)
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _insert_wager(cur, tid, cid, "app.a", _KEY)
            # The same key under ANOTHER app is a different claim.
            _insert_wager(cur, tid, cid, "app.b", _KEY)

    def test_unkeyed_rows_do_not_collide(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            for _ in range(3):
                _insert_wager(cur, tid, cid, "app.legacy", None)

    @pytest.mark.parametrize(
        "bad_key",
        ["", "has space", "tab\there", "semi;colon", "x" * 129],
    )
    def test_the_shape_check_rejects_keys_outside_the_host_alphabet(
        self, pg_db: PgTestDatabase, bad_key: str
    ) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            with pytest.raises(
                (psycopg2.errors.CheckViolation, psycopg2.errors.StringDataRightTruncation)
            ):
                _insert_wager(cur, tid, cid, "app.shape", bad_key)

    def test_ddl_is_idempotent_a_second_application_succeeds(
        self, pg_db: PgTestDatabase
    ) -> None:
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        with _cursor(pg_db) as cur:
            cur.execute(sql)
            cur.execute(sql)


@requires_docker
class TestRolePrivilegeBoundary:
    def test_runtime_role_can_append_keyed_rows_but_never_rewrite_or_delete(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT has_column_privilege(%s, 'economy_ledger', 'idempotency_key', 'INSERT')",
                (_ROLE,),
            )
            assert cur.fetchone() == (True,)
            # Append-only: a key, once written, can be neither moved nor erased
            # by the role (rewriting the key would be a replay-protection bypass).
            for priv in ("UPDATE", "DELETE"):
                cur.execute(
                    "SELECT has_table_privilege(%s, 'economy_ledger', %s)", (_ROLE, priv)
                )
                assert cur.fetchone() == (False,), priv
            cur.execute(
                "SELECT has_column_privilege(%s, 'economy_ledger', 'idempotency_key', 'UPDATE')",
                (_ROLE,),
            )
            assert cur.fetchone() == (False,)
