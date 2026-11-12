"""hub-api reputation writes against a REAL, fully-migrated Postgres (alembic head incl. 0096).

The sqlite suites fake the Postgres-only SQL functions; this file proves the hub-api service layer
actually drives them: `%s` binding of uuid / smallint / NULL / timestamptz parameters through
`AsyncDAL.executesql_async`, the admin override's delta arithmetic against the real stored score,
soft-ban set/clear, the binder<->DDL column agreement for the new tables, and Art. 17 erasure of
every new table. Skipped (never failed) where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import community_reputation_service as svc  # noqa: E402
from services import data_privacy_service as privacy  # noqa: E402

pg_only = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")

USER_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_IDS: dict[str, tuple[int, int]] = {}


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One migrated Postgres 17 with a tenant, a community and one uuid-bearing member."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available")
    with migrated_postgres("hub-api-reputation-pg") as db:
        raw = psycopg2.connect(db.dsn)
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute(
                "ALTER TABLE community_members ADD COLUMN IF NOT EXISTS reputation INTEGER "
                "DEFAULT 600, ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            )
            # legacy reputation tables erasure still clears: from the REAL legacy SQL
            legacy = Path(__file__).resolve().parents[2] / "config" / "postgres" / "migrations"
            for name in ("080_add_reputation_tables.sql", "097_reputation_tenant_scope.sql"):
                cur.execute((legacy / name).read_text())
            cur.execute(
                "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
            )
            cur.execute(
                "INSERT INTO app_catalog (app_id) VALUES ('rep-app') ON CONFLICT DO NOTHING"
            )
            cur.execute("INSERT INTO tenants (slug) VALUES ('rep-pg') RETURNING id")
            tid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES ('c', %s) RETURNING id", (tid,)
            )
            cid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO community_members (community_id, user_id, user_uuid, display_name) "
                "VALUES (%s, '42', %s, 'u42')",
                (cid, USER_UUID),
            )
        raw.close()
        _IDS[db.dsn] = (tid, cid)
        yield db


@pytest.fixture
def adal(pg_db: PgTestDatabase) -> Iterator[AsyncDAL]:
    """Real pydal `AsyncDAL` on the migrated container (`migrate=False`: the schema is real)."""
    from pydal import Field

    async_dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug"),
        Field("is_active", "boolean"),
        Field("logo_url"),
        Field("display_name"),
        Field("is_global", "boolean"),
        Field("config", "json"),
        migrate=False,
    )
    svc._ensure_reputation_tables(dal)  # noqa: SLF001
    yield async_dal
    dal.close()


def _ids(db: PgTestDatabase) -> tuple[int, int]:
    return _IDS[db.dsn]


_USER_TABLES = (
    "bundle_reputation_scores",
    "bundle_reputation_tenant_scores",
    "bundle_reputation_soft_bans",
)


def _count(db: PgTestDatabase, table: str) -> int:
    """Rows keyed by the test subject's uuid in one of the fixed reputation tables."""
    assert table in _USER_TABLES
    return int(
        _q(db, f"SELECT count(*) FROM {table} WHERE user_uuid = %s", (USER_UUID,))[0][0]  # noqa: S608
    )


def _q(db: PgTestDatabase, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


@pg_only
class TestOverrideAndApplyOnPostgres:
    async def test_apply_override_and_clamp_round_trip(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _ids(pg_db)
        dal = adal.dal
        r = await svc.apply_reputation(
            adal,
            tenant_id=tid,
            community_id=cid,
            user_uuid=USER_UUID,
            delta=25,
            reason_code=svc.ReasonCode.ADMIN_ADJUST,
            actor="admin:7",
            source_event="t",
            idempotency_key="k1",
        )
        assert (r.new_score, r.applied) == (625, True)
        replay = await svc.apply_reputation(
            adal,
            tenant_id=tid,
            community_id=cid,
            user_uuid=USER_UUID,
            delta=25,
            reason_code=svc.ReasonCode.ADMIN_ADJUST,
            actor="admin:7",
            source_event="t",
            idempotency_key="k1",
        )
        assert (replay.new_score, replay.applied) == (625, False)

        new, change = await svc.override_member_reputation(
            adal,
            dal,
            tenant_id=tid,
            community_id=cid,
            user_id=42,
            amount=None,
            set_to=900,
            actor_user_id=7,
        )
        assert (new, change) == (850, None)  # clamped to the policy max
        new, change = await svc.override_member_reputation(
            adal,
            dal,
            tenant_id=tid,
            community_id=cid,
            user_id=42,
            amount=-50,
            set_to=None,
            actor_user_id=7,
        )
        assert (new, change) == (800, -50)

        me = await svc.get_my_reputation(adal, dal, community_id=cid, hub_user_id=42, tenant_id=tid)
        assert me.community_score == 800 and me.total_events == 3
        board = await svc.get_leaderboard(adal, dal, community_id=cid)
        assert [(e.display_name, e.score) for e in board] == [("u42", 800)]
        history = await svc.get_history(adal, dal, community_id=cid, user_id=42)
        assert [h.actorType for h in history] == ["admin", "admin", "admin"]

    async def test_policy_upsert_and_soft_ban_round_trip(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _ids(pg_db)
        dal = adal.dal
        cfg = await svc.set_community_config(
            adal,
            dal,
            tenant_id=tid,
            community_id=cid,
            soft_ban_enabled=True,
            soft_ban_floor=520,
            hard_ban_enabled=None,
            hard_ban_floor=380,
            at_risk_threshold=640,
        )
        assert (cfg["soft_ban_enabled"], cfg["soft_ban_floor"], cfg["hard_ban_floor"]) == (
            True,
            520,
            380,
        )
        cfg = await svc.set_community_config(  # second call updates the same row
            adal,
            dal,
            tenant_id=tid,
            community_id=cid,
            soft_ban_enabled=None,
            soft_ban_floor=None,
            hard_ban_enabled=True,
            hard_ban_floor=None,
            at_risk_threshold=None,
        )
        assert cfg["hard_ban_enabled"] is True and cfg["soft_ban_floor"] == 520
        tcfg = await svc.set_tenant_config(
            adal,
            dal,
            tenant_id=tid,
            auto_ban_enabled=True,
            auto_ban_floor=360,
            at_risk_threshold=460,
        )
        assert tcfg["tenant_auto_ban_floor"] == 360

        await svc.set_soft_ban(
            adal,
            dal,
            tenant_id=tid,
            community_id=cid,
            user_id=42,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            actor_user_id=7,
        )
        listed = await svc.list_soft_bans(adal, dal, community_id=cid)
        assert [(b.userId, b.reason, b.setByType) for b in listed] == [("42", "manual", "admin")]
        assert _q(pg_db, "SELECT reputation_is_soft_banned(%s, %s, %s)", (tid, cid, USER_UUID)) == [
            (True,)
        ]
        assert (
            await svc.clear_soft_ban(adal, dal, tenant_id=tid, community_id=cid, user_id=42) is True
        )
        assert (
            await svc.clear_soft_ban(adal, dal, tenant_id=tid, community_id=cid, user_id=42)
            is False
        )

    async def test_erasure_deletes_every_new_table_for_the_subject(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        from services.data_privacy_service import _ErasureScope  # noqa: PLC0415

        tid, cid = _ids(pg_db)
        dal = adal.dal
        await svc.apply_reputation(
            adal,
            tenant_id=tid,
            community_id=cid,
            user_uuid=USER_UUID,
            delta=1,
            reason_code=svc.ReasonCode.ADMIN_ADJUST,
            actor="admin:7",
            source_event="t",
        )
        await svc.set_soft_ban(
            adal, dal, tenant_id=tid, community_id=cid, user_id=42, expires_at=None, actor_user_id=7
        )
        for table in (
            "bundle_reputation_scores",
            "bundle_reputation_tenant_scores",
            "bundle_reputation_soft_bans",
        ):
            assert _count(pg_db, table) >= 1
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_adjustments WHERE target_user_uuid = %s",
                (USER_UUID,),
            )[0][0]
            >= 1
        )

        scope = _ErasureScope(
            identities=(), member_rows=(), user_uuids=(USER_UUID,), restrictions=()
        )
        counts = privacy._delete_reputation_and_identity(dal, scope, user_id=1)  # noqa: SLF001
        dal.commit()
        assert counts["bundle_reputation_tenant_scores"] >= 1
        assert counts["bundle_reputation_soft_bans"] >= 1
        for table in (
            "bundle_reputation_scores",
            "bundle_reputation_tenant_scores",
            "bundle_reputation_soft_bans",
        ):
            assert _count(pg_db, table) == 0
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_adjustments WHERE target_user_uuid = %s",
                (USER_UUID,),
            )[0][0]
            == 0
        )
