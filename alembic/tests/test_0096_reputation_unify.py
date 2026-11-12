"""Tests for 0096_reputation_unify against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head, and `reputation_apply()`: clamp, idempotency,
concurrency, tenant isolation, ledger columns, grants, and the one-shot backfill.
"""

from __future__ import annotations

import importlib.util
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

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
_PATH = _ROOT / "alembic" / "versions" / "0096_reputation_unify.py"
_APPLY = "SELECT new_score, applied FROM reputation_apply(%s, %s, %s, %s, %s::smallint, %s, %s, %s)"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0096", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains(self) -> None:
        m = _load()
        assert len(m.revision) <= 32
        assert m.down_revision == "0095_bundle_disabled_apps_view"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0096-reputation") as db:
        yield db


def _connect(db: PgTestDatabase):  # type: ignore[no-untyped-def]
    conn = psycopg2.connect(
        host=db.host, port=db.port, user=db.user, password=db.password, dbname=db.dbname
    )
    conn.autocommit = True
    return conn


def _scope(db: PgTestDatabase, slug: str) -> tuple[int, int]:
    """Create a tenant + community and return their ids."""
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (slug) VALUES (%s) ON CONFLICT (slug) DO UPDATE "
            "SET slug = EXCLUDED.slug RETURNING id",
            (slug,),
        )
        tid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id", (slug, tid)
        )
        return tid, cur.fetchone()[0]


def _apply(db, tid, cid, user, delta, key=None, actor="bundle:x", reason=1):  # type: ignore[no-untyped-def]
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(_APPLY, (tid, cid, user, delta, reason, actor, "evt", key))
        return cur.fetchone()


@requires_docker
class TestReputationApply:
    def test_first_apply_starts_at_600_and_adds_delta(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-start")
        assert _apply(pg_db, tid, cid, str(uuid.uuid4()), 25) == (625, True)

    def test_clamps_to_300_850(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-clamp")
        user = str(uuid.uuid4())
        assert _apply(pg_db, tid, cid, user, 100000) == (850, True)
        assert _apply(pg_db, tid, cid, user, -100000) == (300, True)
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT delta, requested_value, balance_after, kind FROM "
                "bundle_reputation_adjustments WHERE community_id=%s ORDER BY id",
                (cid,),
            )
            # applied delta is the clamped one; requested is what was asked
            assert cur.fetchall() == [
                (250, 100000, 850, "adjust"),
                (-550, -100000, 300, "adjust"),
            ]

    def test_idempotent_key_replays_without_double_apply(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-idem")
        user = str(uuid.uuid4())
        assert _apply(pg_db, tid, cid, user, 10, key="k1") == (610, True)
        assert _apply(pg_db, tid, cid, user, 10, key="k1") == (610, False)
        assert _apply(pg_db, tid, cid, user, 10, key="k2") == (620, True)

    def test_concurrent_applies_serialize_and_never_lose_updates(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-conc")
        user = str(uuid.uuid4())
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                _apply(pg_db, tid, cid, user, 1, key=f"c{i % 10}")  # 10 keys, each sent twice
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT balance, adjustment_count FROM bundle_reputation_scores "
                "WHERE community_id=%s AND user_uuid=%s",
                (cid, user),
            )
            assert cur.fetchone() == (610, 10)

    def test_tenant_isolation_rejects_foreign_community(self, pg_db: PgTestDatabase) -> None:
        tid_a, _ = _scope(pg_db, "t-iso-a")
        _, cid_b = _scope(pg_db, "t-iso-b")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _apply(pg_db, tid_a, cid_b, str(uuid.uuid4()), 5)

    def test_tenant_aggregate_follows_the_applies(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-agg")
        user = str(uuid.uuid4())
        _apply(pg_db, tid, cid, user, 20)
        _apply(pg_db, tid, cid, user, 5)
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT score, event_count FROM bundle_reputation_tenant_scores "
                "WHERE tenant_id=%s AND user_uuid=%s",
                (tid, user),
            )
            assert cur.fetchone() == (625, 2)

    def test_admin_actor_is_recorded_as_override_with_actor_and_reason(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-admin")
        _apply(pg_db, tid, cid, str(uuid.uuid4()), -30, actor="admin:7", reason=9)
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, actor, source_event, reason_code_id, app_id FROM "
                "bundle_reputation_adjustments WHERE community_id=%s",
                (cid,),
            )
            assert cur.fetchone() == ("admin_override", "admin:7", "evt", 9, None)

    def test_policy_row_changes_the_scale(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-policy")
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bundle_reputation_policy (tenant_id, community_id, score_max) "
                "VALUES (%s, %s, 700)",
                (tid, cid),
            )
        assert _apply(pg_db, tid, cid, str(uuid.uuid4()), 500) == (700, True)

    def test_policy_rejects_inverted_thresholds(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-badpolicy")
        with (
            _connect(pg_db) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg2.errors.CheckViolation),
        ):
            cur.execute(
                "INSERT INTO bundle_reputation_policy "
                "(tenant_id, community_id, hard_ban_floor, soft_ban_floor) "
                "VALUES (%s, %s, 500, 400)",
                (tid, cid),
            )

    def test_public_cannot_execute(self, pg_db: PgTestDatabase) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT has_function_privilege('public', "
                "'reputation_apply(bigint,bigint,uuid,int,smallint,text,text,text)', 'EXECUTE')"
            )
            assert cur.fetchone()[0] is False

    def test_backfill_is_idempotent_and_skips_unresolvable(self, pg_db: PgTestDatabase) -> None:
        _, cid = _scope(pg_db, "t-backfill")
        user = str(uuid.uuid4())
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE community_members ADD COLUMN IF NOT EXISTS reputation NUMERIC")
            cur.execute(
                "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
            )
            cur.execute(
                "INSERT INTO community_members (community_id, user_id, user_uuid, reputation) "
                "VALUES (%s, '42', %s, 777), (%s, '43', NULL, 700)",
                (cid, user, cid),
            )
            for _ in range(2):  # second run must be a no-op
                cur.execute(_load()._BACKFILL_SQL)
            cur.execute(
                "SELECT user_uuid::text, balance FROM bundle_reputation_scores "
                "WHERE community_id=%s",
                (cid,),
            )
            assert cur.fetchall() == [(user, 777)]


def _soft_ban(db, tid, cid, user, reason=2, expires=None):  # type: ignore[no-untyped-def]
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reputation_set_soft_ban(%s, %s, %s, %s::smallint, 'admin:1', %s)",
            (tid, cid, user, reason, expires),
        )


def _is_soft_banned(db, tid, cid, user) -> bool:  # type: ignore[no-untyped-def]
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute("SELECT reputation_is_soft_banned(%s, %s, %s)", (tid, cid, user))
        return bool(cur.fetchone()[0])


@requires_docker
class TestSoftBans:
    def test_set_then_read_then_clear(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-sb1")
        user = str(uuid.uuid4())
        assert _is_soft_banned(pg_db, tid, cid, user) is False
        _soft_ban(pg_db, tid, cid, user)
        assert _is_soft_banned(pg_db, tid, cid, user) is True
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT reputation_clear_soft_ban(%s, %s, %s)", (tid, cid, user))
            assert cur.fetchone()[0] is True
            cur.execute("SELECT reputation_clear_soft_ban(%s, %s, %s)", (tid, cid, user))
            assert cur.fetchone()[0] is False
        assert _is_soft_banned(pg_db, tid, cid, user) is False

    def test_expired_soft_ban_is_not_banned(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-sb2")
        user = str(uuid.uuid4())
        _soft_ban(pg_db, tid, cid, user, expires="2000-01-01T00:00:00Z")
        assert _is_soft_banned(pg_db, tid, cid, user) is False

    def test_is_scoped_to_its_community(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-sb3")
        _, other_cid = _scope(pg_db, "t-sb3b")
        user = str(uuid.uuid4())
        _soft_ban(pg_db, tid, cid, user)
        assert _is_soft_banned(pg_db, tid, other_cid, user) is False

    def test_foreign_community_is_rejected(self, pg_db: PgTestDatabase) -> None:
        tid_a, _ = _scope(pg_db, "t-sb4a")
        _, cid_b = _scope(pg_db, "t-sb4b")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _soft_ban(pg_db, tid_a, cid_b, str(uuid.uuid4()))

    def test_floor_soft_ban_auto_clears_on_recovery_but_manual_never_does(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sb5")
        floor_user, manual_user = str(uuid.uuid4()), str(uuid.uuid4())
        for u in (floor_user, manual_user):
            _apply(pg_db, tid, cid, u, -100)  # 500, below the 550 soft floor
        _soft_ban(pg_db, tid, cid, floor_user, reason=1)
        _soft_ban(pg_db, tid, cid, manual_user, reason=2)
        _apply(pg_db, tid, cid, floor_user, 10)  # 510: still below -> stays banned
        assert _is_soft_banned(pg_db, tid, cid, floor_user) is True
        _apply(pg_db, tid, cid, floor_user, 60)  # 570 >= 550 -> floor ban clears
        _apply(pg_db, tid, cid, manual_user, 200)  # 700 -> manual ban stays
        assert _is_soft_banned(pg_db, tid, cid, floor_user) is False
        assert _is_soft_banned(pg_db, tid, cid, manual_user) is True

    def test_ledger_rows_keep_their_community_for_future_attribution(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-attr")
        _apply(pg_db, tid, cid, str(uuid.uuid4()), 5)
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, community_id FROM bundle_reputation_adjustments "
                "WHERE community_id=%s",
                (cid,),
            )
            assert cur.fetchall() == [(tid, cid)]

    def test_two_communities_each_move_the_tenant_aggregate(self, pg_db: PgTestDatabase) -> None:
        tid, cid_a = _scope(pg_db, "t-two")
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES ('b', %s) RETURNING id", (tid,)
            )
            cid_b = cur.fetchone()[0]
        user = str(uuid.uuid4())
        _apply(pg_db, tid, cid_a, user, -40)
        _apply(pg_db, tid, cid_b, user, -30)
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT score FROM bundle_reputation_tenant_scores WHERE tenant_id=%s AND user_uuid=%s",
                (tid, user),
            )
            assert cur.fetchone()[0] == 530
