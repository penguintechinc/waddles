"""Tests for 0140_floor_ban_unban_observed against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head; the observed-time bound (an unban observed at T never lifts a
ban created after T, so a re-ban survives a late event); the guild-wide lift (every community of the
ONE tenant linked to the guild, never another tenant's, nothing when the guild is contested across
tenants, tenant-scope bans untouched); the mandatory source app; the grants; and the downgrade.
"""

from __future__ import annotations

import importlib.util
import uuid
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
_PATH = _ROOT / "alembic" / "versions" / "0140_floor_ban_unban_observed.py"
_SRC = "system:discord-gateway"
_FAR = 4102444800000  # 2100-01-01, ms
_GUILD_LIFT = (
    "SELECT r_communities, r_lifted, r_soft_cleared FROM "
    "reputation_floor_ban_lift_from_platform_guild(%s, 'discord', %s, %s::uuid, %s, %s)"
)


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0140", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_0139(self) -> None:
        m = _load()
        assert m.revision == "0140_floor_ban_unban_observed"
        assert len(m.revision) <= 32
        assert m.down_revision == "0139_floor_ban_rearm_policy"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f"diverged alembic heads: {heads}"


_COMMUNITY_SERVERS = (
    "CREATE TABLE IF NOT EXISTS community_servers ("
    "id SERIAL PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id) "
    "ON DELETE CASCADE, platform VARCHAR(50) NOT NULL, platform_server_id VARCHAR(255) NOT NULL, "
    "status VARCHAR(50) DEFAULT 'pending', is_primary BOOLEAN DEFAULT false, "
    "UNIQUE (community_id, platform, platform_server_id))"
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0140-unban-observed") as db:
        # Created by the config/postgres base schema (not alembic); same shape as 000_*.sql.
        _q(db, _COMMUNITY_SERVERS)
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


def _tenant(db: PgTestDatabase, slug: str) -> int:
    return int(
        _q(
            db,
            "INSERT INTO tenants (slug) VALUES (%s) ON CONFLICT (slug) DO UPDATE "
            "SET slug = EXCLUDED.slug RETURNING id",
            (slug,),
        )[0][0]
    )


def _community(db: PgTestDatabase, tid: int, name: str, guild: str | None = None) -> int:
    cid = int(
        _q(
            db,
            "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
            (name, tid),
        )[0][0]
    )
    if guild is not None:
        _q(
            db,
            "INSERT INTO community_servers (community_id, platform, platform_server_id, "
            "status, is_primary) VALUES (%s, 'discord', %s, 'approved', false)",
            (cid, guild),
        )
    return cid


def _ban(
    db: PgTestDatabase, tid: int, cid: int, user: str, scope: int = 1, status: int = 2
) -> int:
    return int(
        _q(
            db,
            "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, "
            "origin_community_id, user_uuid, scope, idempotency_key, reason, platform, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'community_hard_floor', 'discord', %s) RETURNING id",
            (tid, cid, cid, user, scope, f"k-{uuid.uuid4().hex[:12]}", status),
        )[0][0]
    )


def _status(db: PgTestDatabase, row_id: int) -> int:
    return int(
        _q(db, "SELECT status FROM bundle_reputation_floor_bans WHERE id = %s", (row_id,))[0][0]
    )


def _guild() -> str:
    return str(uuid.uuid4().int % 10**18 + 10**17)


@requires_docker
class TestObservedTimeBound:
    def test_an_event_observed_before_a_reban_never_lifts_the_new_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-obs-1")
        cid = _community(pg_db, tid, "c-obs-1")
        user = str(uuid.uuid4())
        old = _ban(pg_db, tid, cid, user)
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET created_at = NOW() - interval '10 seconds' "
            "WHERE id = %s",
            (old,),
        )
        observed_ms = int(_q(pg_db, "SELECT (extract(epoch FROM NOW()) * 1000)::bigint")[0][0])
        # The re-ban happens AFTER the unban was observed (T + 1s), then the late event is consumed.
        new = _ban(pg_db, tid, cid, user)
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET created_at = to_timestamp(%s / 1000.0 + 1) "
            "WHERE id = %s",
            (observed_ms, new),
        )
        lifted = _q(
            pg_db,
            "SELECT r_lifted FROM reputation_floor_ban_lift_from_platform("
            "%s, %s, 'discord', %s::uuid, %s, %s)",
            (tid, cid, user, _SRC, observed_ms),
        )[0][0]
        assert lifted == 1
        assert _status(pg_db, old) == 8, "the ban that existed at T is lifted"
        assert _status(pg_db, new) == 2, "the ban created after T must stay"

    def test_a_rearmed_row_is_not_lifted_by_an_unban_observed_before_the_rearm(
        self, pg_db: PgTestDatabase
    ) -> None:
        """Ban, refuse, re-arm through the enqueue ON CONFLICT path at T+5s; unban observed T+2s."""
        tid = _tenant(pg_db, "t-obs-rearm")
        cid = _community(pg_db, tid, "c-obs-rearm")
        user = str(uuid.uuid4())
        key = f"k-{uuid.uuid4().hex[:12]}"
        insert = (
            "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, "
            "origin_community_id, user_uuid, scope, idempotency_key, reason, platform, status) "
            "VALUES (%s, %s, %s, %s, 1, %s, 'community_hard_floor', 'discord', %s) "
            "ON CONFLICT (tenant_id, community_id, user_uuid, scope, action, idempotency_key) "
            "DO UPDATE SET status = EXCLUDED.status RETURNING id"
        )
        rid = _q(pg_db, insert, (tid, cid, tid, user, key, 6))[0][0]
        t0 = int(_q(pg_db, "SELECT (extract(epoch FROM NOW()) * 1000)::bigint")[0][0])
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET created_at = to_timestamp(%s / 1000.0 - 10), "
            "armed_at = to_timestamp(%s / 1000.0 - 10) WHERE id = %s",
            (t0, t0, rid),
        )
        # Re-arm at T+5s via the same ON CONFLICT statement the enqueue function uses.
        assert _q(pg_db, insert, (tid, cid, tid, user, key, 1))[0][0] == rid
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET armed_at = to_timestamp(%s / 1000.0 + 5) "
            "WHERE id = %s",
            (t0, rid),
        )
        armed_before = _q(
            pg_db, "SELECT armed_at FROM bundle_reputation_floor_bans WHERE id = %s", (rid,)
        )[0][0]
        assert armed_before is not None
        lifted = _q(
            pg_db,
            "SELECT r_lifted FROM reputation_floor_ban_lift_from_platform("
            "%s, %s, 'discord', %s::uuid, %s, %s)",
            (tid, cid, user, _SRC, t0 + 2000),
        )[0][0]
        assert lifted == 0, "an unban observed at T+2s must not lift the ban re-armed at T+5s"
        assert _status(pg_db, rid) == 1

    def test_the_trigger_stamps_armed_at_on_a_real_rearm(self, pg_db: PgTestDatabase) -> None:
        tid = _tenant(pg_db, "t-obs-trig")
        cid = _community(pg_db, tid, "c-obs-trig")
        rid = _ban(pg_db, tid, cid, str(uuid.uuid4()), status=6)
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET armed_at = NOW() - interval '1 hour' "
            "WHERE id = %s",
            (rid,),
        )
        _q(pg_db, "UPDATE bundle_reputation_floor_bans SET status = 1 WHERE id = %s", (rid,))
        age = _q(
            pg_db,
            "SELECT extract(epoch FROM NOW() - armed_at) FROM bundle_reputation_floor_bans "
            "WHERE id = %s",
            (rid,),
        )[0][0]
        assert float(age) < 30, "re-arming from status 6 must stamp armed_at = now()"

    def test_a_reban_after_a_lift_restamps_armed_at_and_a_stale_unban_does_not_lift_it(
        self, pg_db: PgTestDatabase
    ) -> None:
        """Lift (8) then re-arm through 8->1 and 8->5: a stale unban must not lift either."""
        for rearm_status in (1, 5):
            tid = _tenant(pg_db, f"t-obs-8to{rearm_status}")
            cid = _community(pg_db, tid, f"c-obs-8to{rearm_status}")
            rid = _ban(pg_db, tid, cid, str(uuid.uuid4()), status=8)
            _q(
                pg_db,
                "UPDATE bundle_reputation_floor_bans SET armed_at = NOW() - interval '1 hour' "
                "WHERE id = %s",
                (rid,),
            )
            stale_ms = int(
                float(_q(pg_db, "SELECT extract(epoch FROM NOW() - interval '1 minute')")[0][0])
                * 1000
            )
            _q(
                pg_db,
                "UPDATE bundle_reputation_floor_bans SET status = %s WHERE id = %s",
                (rearm_status, rid),
            )
            age = _q(
                pg_db,
                "SELECT extract(epoch FROM NOW() - armed_at) FROM bundle_reputation_floor_bans "
                "WHERE id = %s",
                (rid,),
            )[0][0]
            assert float(age) < 30, f"8 -> {rearm_status} must stamp armed_at = now()"
            user = _q(
                pg_db, "SELECT user_uuid FROM bundle_reputation_floor_bans WHERE id = %s", (rid,)
            )[0][0]
            lifted = _q(
                pg_db,
                "SELECT r_lifted FROM reputation_floor_ban_lift_from_platform("
                "%s, %s, 'discord', %s::uuid, %s, %s)",
                (tid, cid, str(user), _SRC, stale_ms),
            )[0][0]
            assert lifted == 0, "an unban observed before the re-arm must not lift it"
            assert _status(pg_db, rid) == rearm_status

    def test_observed_time_and_source_app_are_mandatory(self, pg_db: PgTestDatabase) -> None:
        tid = _tenant(pg_db, "t-obs-2")
        cid = _community(pg_db, tid, "c-obs-2")
        user = str(uuid.uuid4())
        for src, ms in ((None, _FAR), (_SRC, None), (_SRC, 0)):
            with pytest.raises(psycopg2.errors.InvalidParameterValue):
                _q(
                    pg_db,
                    "SELECT * FROM reputation_floor_ban_lift_from_platform("
                    "%s, %s, 'discord', %s::uuid, %s, %s)",
                    (tid, cid, user, src, ms),
                )
        with pytest.raises(psycopg2.errors.UndefinedFunction):  # no stale 5-arg overload
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_lift_from_platform("
                "%s, %s, 'discord', %s::uuid, %s)",
                (tid, cid, user, _SRC),
            )




@requires_docker
class TestGuildWideLift:
    def test_every_community_of_the_tenant_linked_to_the_guild_is_lifted(
        self, pg_db: PgTestDatabase
    ) -> None:
        guild = _guild()
        tid = _tenant(pg_db, "t-guild-1")
        c1 = _community(pg_db, tid, "g1-a", guild)
        c2 = _community(pg_db, tid, "g1-b", guild)
        c3 = _community(pg_db, tid, "g1-unlinked")
        user = str(uuid.uuid4())
        b1, b2, b3 = (_ban(pg_db, tid, c, user) for c in (c1, c2, c3))
        # A tenant-scope ban in a linked community is NOT lifted (scope 1 only).
        tenant_scope = _ban(pg_db, tid, c1, user, scope=2)
        row = _q(pg_db, _GUILD_LIFT, (tid, guild, user, _SRC, _FAR))[0]
        assert tuple(row[:2]) == (2, 2)
        assert _status(pg_db, b1) == 8 and _status(pg_db, b2) == 8
        assert _status(pg_db, b3) == 2, "an unlinked community is untouched"
        assert _status(pg_db, tenant_scope) == 2, "a tenant-scope ban is never lifted"
        # Idempotent.
        assert _q(pg_db, _GUILD_LIFT, (tid, guild, user, _SRC, _FAR))[0][1] == 0

    def test_a_guild_never_lifts_for_another_tenant(self, pg_db: PgTestDatabase) -> None:
        guild = _guild()
        tid = _tenant(pg_db, "t-guild-2")
        other = _tenant(pg_db, "t-guild-2-other")
        _community(pg_db, tid, "g2-a", guild)
        foreign = _community(pg_db, other, "g2-other")
        user = str(uuid.uuid4())
        fb = _ban(pg_db, other, foreign, user)
        # The event names tenant `other`, but the guild is linked only to `tid`.
        row = _q(pg_db, _GUILD_LIFT, (other, guild, user, _SRC, _FAR))[0]
        assert tuple(row) == (0, 0, 0)
        assert _status(pg_db, fb) == 2

    def test_a_guild_claimed_by_two_tenants_lifts_nothing(self, pg_db: PgTestDatabase) -> None:
        guild = _guild()
        t1 = _tenant(pg_db, "t-guild-3a")
        t2 = _tenant(pg_db, "t-guild-3b")
        c1 = _community(pg_db, t1, "g3-a", guild)
        c2 = _community(pg_db, t2, "g3-b", guild)
        user = str(uuid.uuid4())
        b1, b2 = _ban(pg_db, t1, c1, user), _ban(pg_db, t2, c2, user)
        for tid in (t1, t2):
            assert tuple(_q(pg_db, _GUILD_LIFT, (tid, guild, user, _SRC, _FAR))[0]) == (0, 0, 0)
        assert _status(pg_db, b1) == 2 and _status(pg_db, b2) == 2

    def test_the_guild_lift_honours_the_observed_bound(self, pg_db: PgTestDatabase) -> None:
        guild = _guild()
        tid = _tenant(pg_db, "t-guild-4")
        cid = _community(pg_db, tid, "g4-a", guild)
        user = str(uuid.uuid4())
        rid = _ban(pg_db, tid, cid, user)
        assert _q(pg_db, _GUILD_LIFT, (tid, guild, user, _SRC, 1000))[0][1] == 0
        assert _status(pg_db, rid) == 2

    def test_only_the_action_role_may_execute(self, pg_db: PgTestDatabase) -> None:
        rows = _q(
            pg_db,
            "SELECT r.rolname FROM pg_roles r WHERE r.rolname <> 'waddlebot' AND "
            "has_function_privilege(r.rolname, "
            "'reputation_floor_ban_lift_from_platform_guild(bigint,text,text,uuid,text,bigint)',"
            " 'EXECUTE') AND NOT r.rolsuper",
        )
        assert [r[0] for r in rows] == ["waddles_svc_action"]


@requires_docker
class TestRoundTrip:
    def test_downgrade_restores_the_five_arg_lift_then_reupgrades(
        self, pg_db: PgTestDatabase
    ) -> None:
        alembic_cli("downgrade", "0139_floor_ban_rearm_policy", dsn=pg_db.dsn)
        found = _q(
            pg_db,
            "SELECT count(*) FROM pg_proc WHERE proname = "
            "'reputation_floor_ban_lift_from_platform_guild'",
        )[0][0]
        assert found == 0
        tid = _tenant(pg_db, "t-rt")
        cid = _community(pg_db, tid, "c-rt")
        user = str(uuid.uuid4())
        rid = _ban(pg_db, tid, cid, user)
        assert (
            _q(
                pg_db,
                "SELECT r_lifted FROM reputation_floor_ban_lift_from_platform("
                "%s, %s, 'discord', %s::uuid, %s)",
                (tid, cid, user, _SRC),
            )[0][0]
            == 1
        )
        assert _status(pg_db, rid) == 8
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
