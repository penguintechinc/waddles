"""0121_dsar_requests: chain, emitted DDL, and the real-Postgres up/down + append-only guarantees."""

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
_PATH = _ROOT / "alembic" / "versions" / "0121_dsar_requests.py"
_PREV = "0118_room_scopes_moderator"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0121", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0121_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    ancestry = {r.revision for r in script.walk_revisions(base="base", head=heads[0])}
    assert "0121_dsar_requests" in ancestry


def test_upgrade_and_downgrade_emit_expected_ddl() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    up = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS dsar_requests" in up
    assert "CREATE TABLE IF NOT EXISTS dsar_request_events" in up
    assert "public_id                UUID" in up and "BIGSERIAL PRIMARY KEY" in up
    assert up.count("BEFORE UPDATE OR DELETE") == 1
    assert (
        "ix_dsar_requests_tenant_email" in up
        and "(tenant_id, contact_email_hash)" in up
    )
    with mock.patch.object(module.op, "execute") as execute:
        module.downgrade()
    down = [c.args[0] for c in execute.call_args_list]
    assert execute.call_count >= 2
    assert down.index("DROP TABLE IF EXISTS dsar_request_events") < down.index(
        "DROP TABLE IF EXISTS dsar_requests"
    )


@requires_docker
def test_real_postgres_up_down_constraints_and_append_only_events() -> None:
    with migrated_postgres("0121-dsar") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()

            def exists(name: str) -> bool:
                cur.execute("SELECT to_regclass(%s)", (name,))
                return cur.fetchone()[0] is not None

            assert exists("dsar_requests") and exists("dsar_request_events")
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-dsar') RETURNING id")
            tenant_id = cur.fetchone()[0]
            ins = (
                "INSERT INTO dsar_requests (tenant_id, request_type, status, regime, source) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id, public_id"
            )
            cur.execute(ins, (tenant_id, 1, 2, 1, 2))
            req_id, public_id = cur.fetchone()
            assert public_id is not None  # UUID default
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_dsar_requests_tenant_email'"
            )
            assert "(tenant_id, contact_email_hash)" in cur.fetchone()[0]
            for bad in (
                (tenant_id, 0, 1, 1, 1),
                (tenant_id, 1, 9, 1, 1),
                (tenant_id, 1, 1, 4, 1),
                (tenant_id, 1, 1, 1, 0),
            ):
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(ins, bad)
            cur.execute(
                "INSERT INTO dsar_request_events (tenant_id, request_id, event_type, actor_kind) "
                "VALUES (%s, %s, 1, 1) RETURNING id",
                (tenant_id, req_id),
            )
            event_id = cur.fetchone()[0]
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(
                    "UPDATE dsar_request_events SET event_type = 2 WHERE id = %s",
                    (event_id,),
                )
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(
                    "DELETE FROM dsar_request_events WHERE id = %s", (event_id,)
                )
            cur.execute(
                "UPDATE dsar_requests SET verification_token_hash = 'h' WHERE id = %s",
                (req_id,),
            )
            cur.execute(ins, (tenant_id, 1, 2, 1, 2))
            other_id = cur.fetchone()[0]
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "UPDATE dsar_requests SET verification_token_hash = 'h' WHERE id = %s",
                    (other_id,),
                )
            # cascade removal of the parent request is the one permitted way events go away
            cur.execute("DELETE FROM dsar_requests WHERE id = %s", (req_id,))
            cur.execute(
                "SELECT count(*) FROM dsar_request_events WHERE request_id = %s",
                (req_id,),
            )
            assert cur.fetchone()[0] == 0
        finally:
            conn.close()
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT to_regclass('dsar_requests'), to_regclass('dsar_request_events')"
            )
            assert cur.fetchone() == (None, None)
            cur.execute(
                "SELECT 1 FROM pg_proc WHERE proname = 'dsar_request_events_append_only'"
            )
            assert cur.fetchone() is None
        finally:
            conn.close()
        alembic_cli("upgrade", "head", dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('dsar_requests')")
            assert cur.fetchone()[0] is not None
        finally:
            conn.close()
