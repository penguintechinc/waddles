"""0125_tenant_do_not_sell: chain, emitted DDL, and real-Postgres constraints + up/down/up."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from unittest import mock

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0125_tenant_do_not_sell.py"
_REV = "0125_tenant_do_not_sell"
_PREV = "0124_dsar_open_public_unique"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0125", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0125_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    ancestry = {r.revision for r in script.walk_revisions(base="base", head=heads[0])}
    assert _REV in ancestry


def test_upgrade_and_downgrade_emit_expected_ddl() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    up = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS tenant_do_not_sell" in up
    assert "UNIQUE (tenant_id, user_id)" in up
    assert "INSERT INTO" not in up and "UPDATE " not in up  # no data backfill
    with (
        mock.patch.object(module.op, "execute") as execute,
        mock.patch.object(module.op, "get_bind") as get_bind,
    ):
        get_bind.return_value.execute.return_value.scalar.return_value = 0
        module.downgrade()
    assert [c.args[0] for c in execute.call_args_list] == [
        "DROP TABLE IF EXISTS tenant_do_not_sell"
    ]


@pytest.mark.parametrize(
    ("held", "env", "refuses"),
    [(3, None, True), (3, "0", True), (3, "1", False), (0, None, False)],
)
def test_downgrade_refuses_to_drop_recorded_opt_outs_unless_confirmed(
    monkeypatch: pytest.MonkeyPatch, held: int, env: str | None, refuses: bool
) -> None:
    module = _load()
    monkeypatch.delenv("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", raising=False)
    if env is not None:
        monkeypatch.setenv("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", env)
    with (
        mock.patch.object(module.op, "execute") as execute,
        mock.patch.object(module.op, "get_bind") as get_bind,
    ):
        get_bind.return_value.execute.return_value.scalar.return_value = held
        if refuses:
            with pytest.raises(
                RuntimeError, match="Refusing to drop tenant_do_not_sell"
            ):
                module.downgrade()
            execute.assert_not_called()
        else:
            module.downgrade()
            assert execute.call_count == 1


@requires_docker
def test_real_postgres_constraints_and_up_down_up() -> None:
    with migrated_postgres("0125-tenant-dns") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-dns-a') RETURNING id")
            tenant_a = cur.fetchone()[0]
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-dns-b') RETURNING id")
            tenant_b = cur.fetchone()[0]
            cur.execute("INSERT INTO hub_users DEFAULT VALUES RETURNING id")
            user = cur.fetchone()[0]
            ins = (
                "INSERT INTO tenant_do_not_sell (tenant_id, user_id, source_code) "
                "VALUES (%s, %s, %s)"
            )
            cur.execute(ins, (tenant_a, user, 1))
            cur.execute(ins, (tenant_b, user, 2))  # other tenant: allowed
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(ins, (tenant_a, user, 1))
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(ins, (tenant_b + 100, user, 9))
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute(ins, (tenant_a + 1000, user, 1))
            cur.execute("SELECT public_id, created_at FROM tenant_do_not_sell LIMIT 1")
            public_id, created_at = cur.fetchone()
            assert public_id is not None and created_at is not None
        finally:
            conn.close()
        # rows exist: the plain downgrade must refuse and leave the table + rows intact
        with pytest.raises(Exception, match="Refusing to drop tenant_do_not_sell"):
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM tenant_do_not_sell")
            assert cur.fetchone()[0] == 2
        finally:
            conn.close()
        os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
        try:
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
        finally:
            del os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"]
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('tenant_do_not_sell')")
            assert cur.fetchone()[0] is None
        finally:
            conn.close()
        alembic_cli("upgrade", "head", dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('tenant_do_not_sell')")
            assert cur.fetchone()[0] == "tenant_do_not_sell"
        finally:
            conn.close()
