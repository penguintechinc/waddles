"""Tests for 0141_floor_ban_rearm_policy against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head; the tenant policy knob governing the re-arm for community- and
tenant-scope bans alike (default 5 with no policy row); the 1..100 range CHECK; the dropped
per-community column; and the downgrade / re-upgrade round trip.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0141_floor_ban_rearm_policy.py"
_DROP = "SELECT reputation_floor_lift_rearm_drop(%s, %s, %s::smallint)"
_SET = "INSERT INTO bundle_reputation_tenant_policy (tenant_id, lift_rearm_drop) VALUES (%s, %s)"
_COLUMN = (
    "SELECT 1 FROM information_schema.columns WHERE table_name = "
    "'bundle_reputation_policy' AND column_name = 'lift_rearm_drop'"
)


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0141", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_0140(self) -> None:
        m = _load()
        assert m.revision == "0141_floor_ban_rearm_policy"
        assert len(m.revision) <= 32
        assert m.down_revision == "0140_floor_ban_platform_sync"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f"diverged alembic heads: {heads}"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0141-rearm-policy") as db:
        yield db


def _q(db: PgTestDatabase, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    conn = psycopg2.connect(
        host=db.host, port=db.port, user=db.user, password=db.password, dbname=db.dbname
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def _tenant(db: PgTestDatabase, slug: str) -> tuple[int, int]:
    tid = _q(db, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))[0][0]
    cid = _q(
        db,
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (slug, tid),
    )[0][0]
    return tid, cid


@requires_docker
class TestTenantKnob:
    def test_default_is_5_without_a_policy_row(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _tenant(pg_db, "t-rp-default")
        assert _q(pg_db, _DROP, (tid, cid, 1))[0][0] == 5
        assert _q(pg_db, _DROP, (tid, cid, 2))[0][0] == 5

    def test_tenant_knob_governs_community_and_tenant_scope(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _tenant(pg_db, "t-rp-knob")
        _q(pg_db, _SET, (tid, 37))
        assert _q(pg_db, _DROP, (tid, cid, 1))[0][0] == 37
        assert _q(pg_db, _DROP, (tid, cid, 2))[0][0] == 37

    def test_other_tenants_are_unaffected(self, pg_db: PgTestDatabase) -> None:
        a, ca = _tenant(pg_db, "t-rp-iso-a")
        b, cb = _tenant(pg_db, "t-rp-iso-b")
        _q(pg_db, _SET, (a, 50))
        assert _q(pg_db, _DROP, (a, ca, 1))[0][0] == 50
        assert _q(pg_db, _DROP, (b, cb, 1))[0][0] == 5

    @pytest.mark.parametrize("bad", [0, 101, -3])
    def test_range_check_rejects_out_of_range(
        self, pg_db: PgTestDatabase, bad: int
    ) -> None:
        tid, _ = _tenant(pg_db, f"t-rp-bad-{bad + 10}")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _q(pg_db, _SET, (tid, bad))

    @pytest.mark.parametrize("ok", [1, 100])
    def test_range_check_accepts_the_bounds(
        self, pg_db: PgTestDatabase, ok: int
    ) -> None:
        tid, cid = _tenant(pg_db, f"t-rp-ok-{ok}")
        _q(pg_db, _SET, (tid, ok))
        assert _q(pg_db, _DROP, (tid, cid, 1))[0][0] == ok

    def test_per_community_column_is_gone(self, pg_db: PgTestDatabase) -> None:
        assert _q(pg_db, _COLUMN) == []


@requires_docker
class TestRoundTrip:
    def test_downgrade_then_reupgrade(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _tenant(pg_db, "t-rp-roundtrip")
        _q(pg_db, _SET, (tid, 60))
        alembic_cli("downgrade", "-1", dsn=pg_db.dsn)
        assert _q(pg_db, _COLUMN), "per-community column restored"
        assert _q(pg_db, _DROP, (tid, cid, 2))[0][0] == 60
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
        assert _q(pg_db, _DROP, (tid, cid, 1))[0][0] == 60
