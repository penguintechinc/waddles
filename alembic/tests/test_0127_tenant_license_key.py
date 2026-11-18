"""0127_tenant_license_key: chain, emitted DDL, and real-Postgres constraints + roundtrip."""

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
_PATH = _ROOT / "alembic" / "versions" / "0127_tenant_license_key.py"
_REV = "0127_tenant_license_key"
_PREV = "0126_community_tier_allocations"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0127", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0127_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REV in {
        r.revision for r in script.walk_revisions(base="base", head=heads[0])
    }


def test_upgrade_and_downgrade_emit_expected_ddl() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    up = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "CREATE TABLE tenant_license_keys" in up
    assert "IF NOT EXISTS tenant_license_keys" not in up
    assert "lock_timeout" in up and "CHECK (seal_version >= 1)" in up
    assert "ix_tlk_set_by" in up
    assert "license_key_sealed" in up and "license_key_fingerprint" in up
    with (
        mock.patch.object(module.op, "execute") as execute,
        mock.patch.object(module.op, "get_bind") as get_bind,
    ):
        get_bind.return_value.execute.return_value.scalar.return_value = 0
        module.downgrade()
    assert [c.args[0] for c in execute.call_args_list] == [
        "DROP TABLE IF EXISTS tenant_license_keys"
    ]


@pytest.mark.parametrize(
    ("held", "env", "refuses"),
    [(2, None, True), (2, "0", True), (2, "1", False), (0, None, False)],
)
def test_downgrade_refuses_to_drop_stored_keys_unless_confirmed(
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
                RuntimeError, match="Refusing to drop tenant_license_keys"
            ):
                module.downgrade()
            execute.assert_not_called()
        else:
            module.downgrade()
            assert execute.call_count == 1


@requires_docker
def test_real_postgres_constraints_cascade_and_roundtrip() -> None:
    with migrated_postgres("0127-tenant-license-key") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-lic') RETURNING id")
            tenant_id = cur.fetchone()[0]
            ins = (
                "INSERT INTO tenant_license_keys "
                "(tenant_id, license_key_sealed, license_key_fingerprint) VALUES (%s,%s,%s)"
            )
            cur.execute(ins, (tenant_id, "opaque-ciphertext", "abcd1234"))
            cur.execute(
                "SELECT seal_version, license_key_set_at, license_key_set_by "
                "FROM tenant_license_keys WHERE tenant_id = %s",
                (tenant_id,),
            )
            seal_version, set_at, set_by = cur.fetchone()
            assert seal_version == 1 and set_at is not None and set_by is None
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(ins, (tenant_id, "x", "y"))  # one key per tenant
            # 0 would select the unauthenticated (no-AAD) open path: never storable
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "UPDATE tenant_license_keys SET seal_version = 0 WHERE tenant_id = %s",
                    (tenant_id,),
                )
            with pytest.raises(psycopg2.errors.NotNullViolation):
                cur.execute(
                    "INSERT INTO tenant_license_keys (tenant_id, license_key_fingerprint) "
                    "VALUES (%s, 'f')",
                    (tenant_id,),
                )
            # the table holds a key: the downgrade must REFUSE (fail closed), table intact
            with pytest.raises(Exception):  # noqa: B017 - CLI wrapper error type varies
                alembic_cli("downgrade", _PREV, dsn=db.dsn)
            cur.execute("SELECT count(*) FROM tenant_license_keys")
            assert cur.fetchone()[0] == 1
            os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
            try:
                alembic_cli("downgrade", _PREV, dsn=db.dsn)
            finally:
                del os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"]
            cur.execute("SELECT to_regclass('tenant_license_keys')")
            assert cur.fetchone()[0] is None
            alembic_cli("upgrade", "head", dsn=db.dsn)
            cur.execute("SELECT to_regclass('tenant_license_keys')")
            assert cur.fetchone()[0] is not None
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-lic2') RETURNING id")
            t2 = cur.fetchone()[0]
            cur.execute(ins, (t2, "c", "f"))
            cur.execute("DELETE FROM tenants WHERE id = %s", (t2,))
            cur.execute(
                "SELECT count(*) FROM tenant_license_keys WHERE tenant_id = %s", (t2,)
            )
            assert cur.fetchone()[0] == 0  # ON DELETE CASCADE
        finally:
            conn.close()
