"""Tests for 0088_economy_elevated: chain, single head, kinds, audit table, grant id.

Static checks pin the chain; the real-Postgres tests (docker, skipped where the CLI is
absent) run the actual chain to ``head`` and prove the new ledger kinds are admitted (and
unknown ones still refused), the grants table gained its stable ``grant_id``, the audit
table enforces its shape (PII-free reason code, unique call key) and the DDL re-applies.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0088_economy_elevated.py"
_USER = "11111111-2222-4333-8444-555555555555"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0088", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.down_revision == "0087_bundle_data_v2"
    assert len(module.revision) <= 32


def test_history_has_exactly_one_head_and_0088_is_in_it() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0088_economy_elevated" in {r.revision for r in script.walk_revisions()}


def test_ddl_declares_the_kinds_within_the_column_width() -> None:
    sql = _load()._SQL_PATH.read_text(encoding="utf-8")
    for kind in ("transfer_from", "transfer_from_in"):
        assert f"'{kind}'" in sql
        assert len(kind) <= 16  # economy_ledger.kind is VARCHAR(16)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0088-economy-elevated") as db:
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
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"e-{uuid.uuid4()}",))
    tid = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (f"e-c-{uuid.uuid4()}", tid),
    )
    return tid, cur.fetchone()[0]


def _audit(cur: Any, tid: int, cid: int, key: str, reason: str = "steal") -> None:
    cur.execute(
        "INSERT INTO economy_elevated_audit (tenant_id, community_id, caller_app_id, "
        "service_app_id, permission_id, grant_id, op, target_user, amount, reason, "
        "idempotency_key) VALUES (%s, %s, 'app', 'svc', 'economy.transfer_from', 1, "
        "'transfer_from', %s, 5, %s, %s)",
        (tid, cid, _USER, reason, key),
    )


@requires_docker
class TestElevatedSchema:
    @pytest.mark.parametrize("kind", ["transfer_from", "transfer_from_in", "credit"])
    def test_new_and_old_kinds_are_accepted(self, pg_db: PgTestDatabase, kind: str) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            cur.execute(
                "INSERT INTO economy_ledger (tenant_id, community_id, app_id, user_uuid, "
                "kind, delta, balance_after) VALUES (%s, %s, 'app', %s, %s, 1, 1)",
                (tid, cid, _USER, kind),
            )

    def test_unknown_kind_is_still_refused(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO economy_ledger (tenant_id, community_id, app_id, user_uuid, "
                    "kind, delta, balance_after) VALUES (%s, %s, 'app', %s, 'steal', 1, 1)",
                    (tid, cid, _USER),
                )

    def test_grants_table_has_a_stable_numeric_grant_id(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'community_permission_grants' AND column_name = 'grant_id'"
            )
            row = cur.fetchone()
            assert row is not None and row[0] == "bigint"

    def test_audit_rejects_a_non_code_reason_and_a_duplicate_call_key(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            tid, cid = _scope(cur)
            _audit(cur, tid, cid, "k1")
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _audit(cur, tid, cid, "k1")
            with pytest.raises(psycopg2.errors.CheckViolation):
                _audit(cur, tid, cid, "k2", reason="Free text with spaces & PII")

    def test_ddl_is_re_appliable(self, pg_db: PgTestDatabase) -> None:
        sql = _load()._SQL_PATH.read_text(encoding="utf-8")
        with _cursor(pg_db) as cur:
            cur.execute(sql)
            cur.execute(sql)
