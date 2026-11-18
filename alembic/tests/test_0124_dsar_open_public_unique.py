"""0124_dsar_open_public_unique: chain, emitted DDL, and real-Postgres uniqueness + downgrade."""

from __future__ import annotations

import importlib.util
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
_PATH = _ROOT / "alembic" / "versions" / "0124_dsar_open_public_unique.py"
_REV = "0124_dsar_open_public_unique"
_PREV = "0123_dsar_mail_outbox"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0124", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0124_is_in_its_ancestry() -> None:
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
    assert "UPDATE dsar_requests" in up  # duplicates resolved BEFORE the index is built
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_dsar_requests_open_public" in up
    assert (
        "(tenant_id, contact_email_hash, request_type)" in up
        and "WHERE status = 2" in up
    )
    with mock.patch.object(module.op, "execute") as execute:
        module.downgrade()
    assert [c.args[0] for c in execute.call_args_list] == [
        "DROP INDEX IF EXISTS ux_dsar_requests_open_public"
    ]


@requires_docker
def test_real_postgres_unique_open_requests_and_downgrade() -> None:
    with migrated_postgres("0124-dsar-open-unique") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-open') RETURNING id")
            tenant_id = cur.fetchone()[0]
            ins = (
                "INSERT INTO dsar_requests "
                "(tenant_id, contact_email_hash, request_type, status, regime, source) "
                "VALUES (%s, %s, %s, %s, 1, 2)"
            )
            cur.execute(ins, (tenant_id, "h1", 1, 2))
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(ins, (tenant_id, "h1", 1, 2))  # same open request twice
            cur.execute(ins, (tenant_id, "h1", 2, 2))  # other type: allowed
            cur.execute(ins, (tenant_id, "h2", 1, 2))  # other email: allowed
            cur.execute(
                ins, (tenant_id, "h1", 1, 3)
            )  # verified rows are outside the index
            cur.execute(ins, (tenant_id, "h1", 1, 3))
            cur.execute("SELECT to_regclass('ux_dsar_requests_open_public')")
            assert cur.fetchone()[0] == "ux_dsar_requests_open_public"
        finally:
            conn.close()
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('ux_dsar_requests_open_public')")
            assert cur.fetchone()[0] is None
        finally:
            conn.close()
