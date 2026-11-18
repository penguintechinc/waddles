"""0126_community_tier_allocations: chain, emitted DDL, and real-Postgres constraints + backfill."""

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
_PATH = _ROOT / "alembic" / "versions" / "0126_community_tier_allocations.py"
_REV = "0126_community_tier_allocations"
_PREV = "0125_tenant_do_not_sell"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0126", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0126_is_in_its_ancestry() -> None:
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
    assert "CREATE TABLE community_tier_allocations" in up
    assert "CREATE TABLE tenant_license_pools" in up
    assert "IF NOT EXISTS community_tier_allocations" not in up
    assert "lock_timeout" in up and "ix_cta_community" in up and "ix_tlp_set_by" in up
    assert "ux_cta_active_community" in up and "WHERE released_at IS NULL" in up
    assert "ADD COLUMN public_id" not in up  # communities.public_id is 0084's
    with (
        mock.patch.object(module.op, "execute") as execute,
        mock.patch.object(module.op, "get_bind") as get_bind,
    ):
        get_bind.return_value.execute.return_value.scalar.return_value = 0
        module.downgrade()
    assert [c.args[0] for c in execute.call_args_list] == [
        "DROP TABLE IF EXISTS tenant_license_pools",
        "DROP TABLE IF EXISTS community_tier_allocations",
    ]


@pytest.mark.parametrize(
    ("held", "env", "refuses"),
    [(4, None, True), (4, "0", True), (4, "1", False), (0, None, False)],
)
def test_downgrade_refuses_to_drop_ledger_unless_confirmed(
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
            with pytest.raises(RuntimeError, match="Refusing to drop"):
                module.downgrade()
            execute.assert_not_called()
        else:
            module.downgrade()
            assert execute.call_count == 2


@requires_docker
def test_real_postgres_constraints_backfill_idempotent_and_roundtrip() -> None:
    with migrated_postgres("0126-community-tier") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-tier') RETURNING id")
            tenant_id = cur.fetchone()[0]
            ids = {}
            for name, premium, tier in (
                ("prem", True, None),
                ("free", False, None),
                ("nul", None, None),
                ("ent", True, "enterprise"),
                ("pro", True, "Professional"),
                ("tieronly", None, "enterprise"),
            ):
                cur.execute(
                    "INSERT INTO communities (name, tenant_id, is_premium, license_tier) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    (name, tenant_id, premium, tier),
                )
                ids[name] = cur.fetchone()[0]
            os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
            try:
                alembic_cli("downgrade", _PREV, dsn=db.dsn)
            finally:
                del os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"]
            cur.execute("SELECT to_regclass('community_tier_allocations')")
            assert cur.fetchone()[0] is None
            alembic_cli("upgrade", "head", dsn=db.dsn)
            cur.execute(
                "SELECT community_id, tier_code, source_code, released_at "
                "FROM community_tier_allocations ORDER BY community_id"
            )
            rows = cur.fetchall()
            # Enterprise stays Enterprise; Professional/unknown-premium -> Professional
            assert sorted((r[0], r[1], r[2], r[3]) for r in rows) == sorted(
                [
                    (ids["prem"], 2, 3, None),
                    (ids["ent"], 3, 3, None),
                    (ids["pro"], 2, 3, None),
                    (ids["tieronly"], 3, 3, None),
                ]
            )

            # idempotent re-run of the backfill statement adds nothing
            module = _load()
            cur.execute(module._UPGRADE[-1])
            cur.execute("SELECT count(*) FROM community_tier_allocations")
            assert cur.fetchone()[0] == 4

            ins = (
                "INSERT INTO community_tier_allocations "
                "(tenant_id, community_id, tier_code, source_code) VALUES (%s,%s,%s,%s)"
            )
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    ins, (tenant_id, ids["free"], 1, 1)
                )  # tier 1 (free) never stored
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(ins, (tenant_id, ids["free"], 4, 1))
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(ins, (tenant_id, ids["free"], 2, 9))
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(ins, (tenant_id, ids["prem"], 3, 1))  # 2nd active row
            cur.execute(
                "UPDATE community_tier_allocations SET released_at = now(), "
                "release_reason_code = 1 WHERE community_id = %s",
                (ids["prem"],),
            )
            cur.execute(ins, (tenant_id, ids["prem"], 3, 1))  # allowed once released
            cur.execute("SELECT public_id FROM community_tier_allocations LIMIT 1")
            assert cur.fetchone()[0] is not None

            pool = (
                "INSERT INTO tenant_license_pools "
                "(tenant_id, axis_code, tier_code, limit_count, source_code) "
                "VALUES (%s,%s,%s,%s,%s)"
            )
            cur.execute(pool, (tenant_id, 3, 2, -1, 1))
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(pool, (tenant_id, 3, 2, 5, 1))
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(pool, (tenant_id, 1, 3, 5, 1))  # only the team axis
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(pool, (tenant_id, 3, 3, -2, 1))
            cur.execute(pool, (tenant_id, 3, 3, None, 3))  # NULL = not licensed
            # the downgrade must REFUSE while the ledger/pools hold rows
            with pytest.raises(Exception):  # noqa: B017 - CLI wrapper error type varies
                alembic_cli("downgrade", _PREV, dsn=db.dsn)
            cur.execute("SELECT count(*) FROM tenant_license_pools")
            assert cur.fetchone()[0] == 2
        finally:
            conn.close()
        os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
        try:
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
        finally:
            del os.environ["ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"]
        alembic_cli("upgrade", "head", dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM community_tier_allocations")
            assert cur.fetchone()[0] == 4  # backfill ran again on the fresh table
            cur.execute("SELECT is_premium FROM communities WHERE name = 'prem'")
            assert (
                cur.fetchone()[0] is True
            )  # denormalized columns untouched by downgrade
        finally:
            conn.close()
