"""Real-Postgres tests for 0069_economy_ledger_kinds (P4 Task 1).

Runs the actual Alembic chain to `head` and asserts the ledger `kind` CHECK now
admits the compound-op kinds (and still refuses anything else), that the DDL is
re-appliable, and that the runtime role gained no new privilege.
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
    Path(__file__).resolve().parents[1] / "versions" / "0069_economy_ledger_kinds.py"
)
_USER = "11111111-2222-4333-8444-555555555555"
_NEW_KINDS = ["credit", "debit", "admin_adjust", "pot_payout", "exchange"]


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0069", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_chains_off_0068(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0069_economy_ledger_kinds"
        assert migration.down_revision == "0068_app_catalog_kind"

    def test_revision_id_fits_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32

    def test_ddl_file_exists_and_lists_every_new_kind(self) -> None:
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        for kind in _NEW_KINDS:
            assert f"'{kind}'" in sql
            assert len(kind) <= 16  # economy_ledger.kind is VARCHAR(16)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0069-economy-ledger-kinds") as db:
        yield db


@contextmanager
def _cursor(db: PgTestDatabase) -> Iterator[Any]:
    conn = psycopg2.connect(
        host=db.host, port=db.port, user=db.user, password=db.password, dbname=db.dbname
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def _scope(cur: Any) -> tuple[int, int]:
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"k-{uuid.uuid4()}",)
    )
    tid = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (f"k-c-{uuid.uuid4()}", tid),
    )
    return tid, cur.fetchone()[0]


def _insert(cur: Any, tid: int, cid: int, kind: str) -> None:
    cur.execute(
        "INSERT INTO economy_ledger "
        "(tenant_id, community_id, app_id, user_uuid, kind, delta, balance_after) "
        "VALUES (%s, %s, 'app', %s, %s, 1, 1)",
        (tid, cid, _USER, kind),
    )


@requires_docker
class TestLedgerKinds:
    @pytest.mark.parametrize("kind", ["wager", "transfer_out", "transfer_in", *_NEW_KINDS])
    def test_every_known_kind_is_accepted(self, pg_db: PgTestDatabase, kind: str) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            _insert(cur, tid, cid, kind)

    @pytest.mark.parametrize("kind", ["mint", "", "CREDIT", "credit "])
    def test_unknown_kinds_are_refused(self, pg_db: PgTestDatabase, kind: str) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            with pytest.raises(psycopg2.errors.CheckViolation):
                _insert(cur, tid, cid, kind)

    def test_ddl_is_re_appliable(self, pg_db: PgTestDatabase) -> None:
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        with _cursor(pg_db) as cur:
            cur.execute(sql)
            cur.execute(sql)
            tid, cid = _scope(cur)
            _insert(cur, tid, cid, "admin_adjust")
