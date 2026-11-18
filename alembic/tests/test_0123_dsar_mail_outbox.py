"""0123_dsar_mail_outbox: chain, emitted DDL, and real-Postgres up/down."""

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
_PATH = _ROOT / "alembic" / "versions" / "0123_dsar_mail_outbox.py"
_REV = "0123_dsar_mail_outbox"
_PREV = "0122_ingest_sources_public_id"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0123", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0123_is_in_its_ancestry() -> None:
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
    assert "CREATE TABLE IF NOT EXISTS dsar_mail_outbox" in up
    assert "WHERE status = 1" in up and "token_enc" in up
    assert "REFERENCES" not in up  # decoy rows need FK-free tenant/request ids
    with mock.patch.object(module.op, "execute") as execute:
        module.downgrade()
    assert [c.args[0] for c in execute.call_args_list] == [
        "DROP TABLE IF EXISTS dsar_mail_outbox"
    ]


@requires_docker
def test_real_postgres_up_down_and_check_constraints() -> None:
    with migrated_postgres("0123-dsar-outbox") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('dsar_mail_outbox')")
            assert cur.fetchone()[0] == "dsar_mail_outbox"
            ins = (
                "INSERT INTO dsar_mail_outbox (tenant_id, request_id, tenant_slug, kind, status) "
                "VALUES (0, 0, 'x', %s, %s) RETURNING public_id, attempts, next_attempt_at"
            )
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'dsar_mail_outbox' AND column_name = 'lease_token'"
            )
            assert (
                cur.fetchone() is not None
            )  # per-claim lease token (stale-worker guard)
            cur.execute(ins, (1, 1))  # no FK: a decoy-shaped row is accepted
            public_id, attempts, due = cur.fetchone()
            assert public_id is not None and attempts == 0 and due is not None
            for bad in ((9, 1), (1, 0), (1, 5)):
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(ins, bad)
        finally:
            conn.close()
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('dsar_mail_outbox')")
            assert cur.fetchone()[0] is None
        finally:
            conn.close()
