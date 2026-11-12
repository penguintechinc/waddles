"""Tests for 0102_reputation_floor_ban against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head, `reputation_verify_floor_breach()` (community hard and tenant
scopes, switch off, floor boundaries, unscored default, tenant pairing, fail-closed inputs), the
outbox functions (enqueue with SQL re-verification + exact quota under concurrency; lease/claim,
recheck, complete-by-token, dead reaping), the tenant fan-out set, and each stage role's
privilege boundary (EXECUTE on its own functions, no direct table access).
"""

from __future__ import annotations

import importlib.util
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
_PATH = _ROOT / "alembic" / "versions" / "0102_reputation_floor_ban.py"
_BUNDLE_LOGIN = "rep_ban_bundle"
_LOGIN_PW = "rep-ban-test-only-pw"
_VERIFY = "SELECT reputation_verify_floor_breach(%s, %s, %s::uuid, %s::smallint)"
_ACTION_LOGIN = "rep_ban_action"
_ENQUEUE = (
    "SELECT r_state, r_id FROM reputation_floor_ban_enqueue("
    "%s, %s, %s, %s::uuid, %s::smallint, %s, %s)"
)
_CLAIM = (
    "SELECT r_id, r_tenant_id, r_community_id, r_user_uuid, r_scope, r_platform, r_attempt, "
    "r_lease_token FROM reputation_floor_ban_claim(%s, %s)"
)
_COMPLETE = (
    "SELECT reputation_floor_ban_complete(%s, %s::uuid, %s::smallint, %s, %s)"
)


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0102", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_0101(self) -> None:
        m = _load()
        assert m.revision == "0102_reputation_floor_ban"
        assert len(m.revision) <= 32
        assert m.down_revision == "0101_bundle_precompile_requests"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        assert ScriptDirectory.from_config(cfg).get_heads() == [
            "0102_reputation_floor_ban"
        ]


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0102-floor-ban") as db:
        with _connect(db) as conn, conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
            )
            cur.execute(
                f"CREATE ROLE {_BUNDLE_LOGIN} LOGIN PASSWORD %s IN ROLE waddles_bundle_reputation",
                (_LOGIN_PW,),
            )
            cur.execute(
                f"CREATE ROLE {_ACTION_LOGIN} LOGIN PASSWORD %s IN ROLE waddles_svc_action",
                (_LOGIN_PW,),
            )
        yield db


def _connect(db: PgTestDatabase, login: str | None = None):  # type: ignore[no-untyped-def]
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=login or db.user,
        password=_LOGIN_PW if login else db.password,
        dbname=db.dbname,
    )
    conn.autocommit = True
    return conn


def _q(
    db: PgTestDatabase, sql: str, args: tuple[Any, ...] = (), login: str | None = None
) -> list[Any]:
    with _connect(db, login) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _tenant(db: PgTestDatabase, slug: str) -> int:
    return _q(
        db,
        "INSERT INTO tenants (slug) VALUES (%s) ON CONFLICT (slug) DO UPDATE "
        "SET slug = EXCLUDED.slug RETURNING id",
        (slug,),
    )[0][0]


def _community(db: PgTestDatabase, tid: int, name: str) -> int:
    return _q(
        db,
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (name, tid),
    )[0][0]


def _score(db: PgTestDatabase, tid: int, cid: int, user: str, balance: int) -> None:
    _q(
        db,
        "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
        "VALUES (%s, %s, %s, %s)",
        (tid, cid, user, balance),
    )


def _tenant_score(db: PgTestDatabase, tid: int, user: str, score: int) -> None:
    _q(
        db,
        "INSERT INTO bundle_reputation_tenant_scores (tenant_id, user_uuid, score) "
        "VALUES (%s, %s, %s)",
        (tid, user, score),
    )


def _verify(
    db: PgTestDatabase,
    tid: int,
    cid: int,
    user: str,
    scope: int,
    login: str | None = None,
) -> bool:
    return _q(db, _VERIFY, (tid, cid, user, scope), login)[0][0]


@requires_docker
class TestVerifyFloorBreach:
    def test_community_hard_requires_enabled_switch_and_score_below_floor(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-vf-comm")
        cid = _community(pg_db, tid, "c1")
        user = str(uuid.uuid4())
        _score(pg_db, tid, cid, user, 350)
        # No policy row (defaults: hard ban OFF) -> never a breach.
        assert _verify(pg_db, tid, cid, user, 1) is False
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled, "
            "hard_ban_floor) VALUES (%s, %s, false, 400)",
            (tid, cid),
        )
        assert _verify(pg_db, tid, cid, user, 1) is False, "switch off"
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET hard_ban_enabled = true WHERE community_id=%s",
            (cid,),
        )
        assert _verify(pg_db, tid, cid, user, 1) is True
        # Boundary: exactly AT the floor is not below it; one under is.
        _q(
            pg_db,
            "UPDATE bundle_reputation_scores SET balance = 400 WHERE community_id=%s",
            (cid,),
        )
        assert _verify(pg_db, tid, cid, user, 1) is False
        _q(
            pg_db,
            "UPDATE bundle_reputation_scores SET balance = 399 WHERE community_id=%s",
            (cid,),
        )
        assert _verify(pg_db, tid, cid, user, 1) is True

    def test_unscored_member_counts_at_the_policy_default_not_as_a_breach(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-vf-unscored")
        cid = _community(pg_db, tid, "c1")
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled) "
            "VALUES (%s, %s, true)",
            (tid, cid),
        )
        assert _verify(pg_db, tid, cid, str(uuid.uuid4()), 1) is False  # 600 > 400

    def test_tenant_scope_uses_the_fixed_aggregate_and_its_own_switch(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-vf-tenant")
        cid = _community(pg_db, tid, "c1")
        other = _community(pg_db, tid, "c2")
        user = str(uuid.uuid4())
        _tenant_score(pg_db, tid, user, 320)
        assert _verify(pg_db, tid, cid, user, 2) is False, "no tenant policy"
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_tenant_policy (tenant_id, tenant_auto_ban_enabled, "
            "tenant_auto_ban_floor) VALUES (%s, false, 350)",
            (tid,),
        )
        assert _verify(pg_db, tid, cid, user, 2) is False, "switch off"
        _q(
            pg_db,
            "UPDATE bundle_reputation_tenant_policy SET tenant_auto_ban_enabled = true "
            "WHERE tenant_id=%s",
            (tid,),
        )
        # True for EVERY community of the tenant (the fan-out re-verifies per target).
        assert _verify(pg_db, tid, cid, user, 2) is True
        assert _verify(pg_db, tid, other, user, 2) is True
        _q(
            pg_db,
            "UPDATE bundle_reputation_tenant_scores SET score = 350 WHERE tenant_id=%s",
            (tid,),
        )
        assert _verify(pg_db, tid, cid, user, 2) is False, (
            "at the floor is not below it"
        )
        # A community-scope hard switch has no bearing on the tenant scope, and vice versa.
        assert _verify(pg_db, tid, cid, user, 1) is False

    def test_fail_closed_on_wrong_tenant_unknown_scope_and_nulls(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-vf-closed")
        other_tid = _tenant(pg_db, "t-vf-closed-other")
        cid = _community(pg_db, tid, "c1")
        user = str(uuid.uuid4())
        _score(pg_db, tid, cid, user, 300)
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled) "
            "VALUES (%s, %s, true)",
            (tid, cid),
        )
        assert _verify(pg_db, tid, cid, user, 1) is True
        assert _verify(pg_db, other_tid, cid, user, 1) is False, (
            "community not in tenant"
        )
        for scope in (0, 3, -1):
            assert _verify(pg_db, tid, cid, user, scope) is False
        assert _q(pg_db, _VERIFY, (tid, cid, user, None))[0][0] is False

    def test_the_bundle_role_may_execute_it(self, pg_db: PgTestDatabase) -> None:
        tid = _tenant(pg_db, "t-vf-role")
        cid = _community(pg_db, tid, "c1")
        assert _verify(pg_db, tid, cid, str(uuid.uuid4()), 1, _BUNDLE_LOGIN) is False


@requires_docker
class TestFloorBanOutbox:
    """Enqueue (process stage) -> claim/recheck/complete (action stage) on a real Postgres."""

    @pytest.fixture(autouse=True)
    def _empty_outbox(self, pg_db: PgTestDatabase) -> None:
        """Claims are global (cross-tenant), so each test starts from an empty, un-halted outbox."""
        _q(pg_db, "TRUNCATE bundle_reputation_floor_bans")
        _q(pg_db, "SELECT reputation_floor_ban_set_halted(false, 'test reset')")

    def _slot(
        self, db: PgTestDatabase, name: str, *, score: int = 350
    ) -> tuple[int, int, str]:
        """A tenant+community with the hard ban ON (floor 400) and a user scoring `score`."""
        tid = _tenant(db, name)
        cid = _community(db, tid, "c1")
        user = str(uuid.uuid4())
        _q(
            db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled) "
            "VALUES (%s, %s, true)",
            (tid, cid),
        )
        _score(db, tid, cid, user, score)
        return tid, cid, user

    def _enqueue(
        self,
        db: PgTestDatabase,
        tid: int,
        cid: int,
        user: str,
        *,
        scope: int = 1,
        key: str = "fb:evt1",
        platform: str = "discord",
        origin: int | None = None,
        login: str | None = _BUNDLE_LOGIN,
    ) -> tuple[int, int]:
        row = _q(
            db,
            _ENQUEUE,
            (
                tid,
                cid,
                origin if origin is not None else cid,
                user,
                scope,
                key,
                platform,
            ),
            login,
        )[0]
        return row[0], row[1]

    def _claim(
        self, db: PgTestDatabase, limit: int = 50, lease: int = 120
    ) -> list[tuple[Any, ...]]:
        return _q(db, _CLAIM, (limit, lease), _ACTION_LOGIN)

    def _complete(
        self,
        db: PgTestDatabase,
        row_id: int,
        lease: str,
        outcome: int,
        error: str = "",
        op_id: str = "",
    ) -> int:
        return _q(db, _COMPLETE, (row_id, lease, outcome, error, op_id), _ACTION_LOGIN)[
            0
        ][0]

    def _row(self, db: PgTestDatabase, row_id: int) -> tuple[Any, ...]:
        return _q(
            db,
            "SELECT status, attempts, last_error, actor, reason, platform, "
            "next_retry_at IS NOT NULL, completed_at IS NOT NULL, lease_token IS NOT NULL, "
            "op_id FROM bundle_reputation_floor_bans WHERE id=%s",
            (row_id,),
        )[0]

    def _mine(self, claimed: list[tuple[Any, ...]], row_id: int) -> tuple[Any, ...]:
        return next(r for r in claimed if r[0] == row_id)

    # --- enqueue --------------------------------------------------------------------------

    def test_enqueue_records_a_queued_audit_row_and_a_replay_is_a_no_op(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-audit")
        state, row_id = self._enqueue(pg_db, tid, cid, user)
        assert state == 0
        status, attempts, err, actor, reason, platform, retry, done, leased, op = (
            self._row(pg_db, row_id)
        )
        assert (status, attempts, err) == (1, 0, None)
        assert (actor, reason, platform) == (
            "system:reputation_floor",
            "community_hard_floor",
            "discord",
        )
        assert (retry, done, leased, op) == (False, False, False, None)
        # The replay (same slot) names the SAME row (the correlation id) and changes nothing.
        assert self._enqueue(pg_db, tid, cid, user) == (1, row_id)
        assert self._row(pg_db, row_id)[:2] == (1, 0)
        # A different key / scope / user is a distinct slot.
        assert self._enqueue(pg_db, tid, cid, user, key="fb:evt2")[0] == 0
        assert self._enqueue(pg_db, tid, cid, user, scope=2)[0] == 3, (
            "tenant scope has no tenant policy: refused by the SQL re-verification"
        )

    def test_m1_the_breach_is_reverified_in_sql_at_enqueue_and_a_refusal_is_recorded(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-m1", score=900)
        state, row_id = self._enqueue(pg_db, tid, cid, user)
        assert state == 3, "score is NOT below the floor: refused, never queued"
        assert self._row(pg_db, row_id)[:3] == (6, 0, "floor_not_breached")
        assert self._claim(pg_db) == [], "a refused row is never claimable"
        # Switch off: refused too, whatever the caller believed.
        _q(
            pg_db,
            "UPDATE bundle_reputation_scores SET balance = 100 WHERE community_id=%s",
            (cid,),
        )
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET hard_ban_enabled = false "
            "WHERE community_id=%s",
            (cid,),
        )
        assert self._enqueue(pg_db, tid, cid, user, key="fb:evt2")[0] == 3
        # Back on and breached: a fresh slot queues.
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET hard_ban_enabled = true "
            "WHERE community_id=%s",
            (cid,),
        )
        assert self._enqueue(pg_db, tid, cid, user, key="fb:evt3")[0] == 0
        # A refused slot re-evaluates on replay (a score that dropped since can now queue).
        assert self._enqueue(pg_db, tid, cid, user, key="fb:evt2")[0] == 0

    def test_tenant_scope_fans_out_one_row_per_target_community_naming_the_origin(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-ob-tenant")
        origin = _community(pg_db, tid, "origin")
        target = _community(pg_db, tid, "target")
        user = str(uuid.uuid4())
        _tenant_score(pg_db, tid, user, 320)
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_tenant_policy (tenant_id, tenant_auto_ban_enabled, "
            "tenant_auto_ban_floor) VALUES (%s, true, 350)",
            (tid,),
        )
        ids = [
            self._enqueue(pg_db, tid, c, user, scope=2, key="fb:t1", origin=origin)
            for c in (origin, target)
        ]
        assert [s for s, _ in ids] == [0, 0]
        rows = _q(
            pg_db,
            "SELECT community_id, origin_community_id, scope, reason FROM "
            "bundle_reputation_floor_bans WHERE user_uuid=%s ORDER BY community_id",
            (user,),
        )
        assert rows == [
            (origin, origin, 2, "tenant_floor"),
            (target, origin, 2, "tenant_floor"),
        ]

    def test_m2_m3_the_hourly_quota_is_exact_under_concurrency_and_recorded(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-ob-quota")
        cid = _community(pg_db, tid, "c1")
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled, "
            "floor_ban_hourly_quota) VALUES (%s, %s, true, 3)",
            (tid, cid),
        )
        users = [str(uuid.uuid4()) for _ in range(12)]
        for u in users:
            _score(pg_db, tid, cid, u, 300)
        results: list[int] = []
        lock = threading.Lock()

        def worker(u: str) -> None:
            state, _ = self._enqueue(pg_db, tid, cid, u, key=f"fb:{u[:8]}")
            with lock:
                results.append(state)

        threads = [threading.Thread(target=worker, args=(u,)) for u in users]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [0] * 3 + [2] * 9, (
            "the per-community advisory lock makes the count exact: exactly the quota queues"
        )
        limited = _q(
            pg_db,
            "SELECT status, last_error, next_retry_at > NOW() FROM bundle_reputation_floor_bans "
            "WHERE community_id=%s AND status=5",
            (cid,),
        )
        assert len(limited) == 9
        assert all(r == (5, "rate_limited", True) for r in limited), (
            "recorded for retry AFTER the quota window, never silently dropped"
        )
        # Another community is unaffected.
        other = _community(pg_db, tid, "c-other")
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled) "
            "VALUES (%s, %s, true)",
            (tid, other),
        )
        _score(pg_db, tid, other, users[0], 300)
        assert self._enqueue(pg_db, tid, other, users[0], key="fb:other")[0] == 0

    def test_a_rate_limited_row_is_retried_only_after_the_window(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, _u = self._slot(pg_db, "t-ob-window")
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET floor_ban_hourly_quota = 1 "
            "WHERE community_id=%s",
            (cid,),
        )
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        _score(pg_db, tid, cid, first, 300)
        _score(pg_db, tid, cid, second, 300)
        s1, id1 = self._enqueue(pg_db, tid, cid, first, key="fb:a")
        s2, id2 = self._enqueue(pg_db, tid, cid, second, key="fb:b")
        assert (s1, s2) == (0, 2)
        claimed = self._claim(pg_db)
        assert [r[0] for r in claimed] == [id1], "the limited row is NOT claimable yet"
        # The window passes (the first ban is old): the limited row becomes due and is claimed.
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET created_at = NOW() - INTERVAL '2 hours', "
            "dispatched_at = CASE WHEN dispatched_at IS NULL THEN NULL "
            "ELSE NOW() - INTERVAL '2 hours' END, "
            "next_retry_at = CASE WHEN status = 5 THEN NOW() - INTERVAL '1 second' "
            "ELSE next_retry_at END WHERE id IN (%s, %s)",
            (id1, id2),
        )
        later = self._claim(pg_db)
        assert [r[0] for r in later] == [id2]
        assert self._row(pg_db, id2)[0] == 7

    # --- claim / lease --------------------------------------------------------------------

    def test_claim_leases_a_queued_row_once_and_only_the_token_holder_completes_it(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-lease")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        claimed = self._claim(pg_db)
        mine = self._mine(claimed, row_id)
        # (id, tenant, community, user, scope, platform, attempt, lease)
        assert mine[1:7] == (tid, cid, user, 1, "discord", 1)
        token = str(mine[7])
        assert self._row(pg_db, row_id)[0] == 7
        # L2: a second claimant (another replica) gets nothing while the lease is live.
        assert self._claim(pg_db) == []
        # A wrong token cannot complete it.
        assert self._complete(pg_db, row_id, str(uuid.uuid4()), 1) == 0
        assert self._row(pg_db, row_id)[0] == 7
        # The holder completes it: done ONLY via the confirmed-result call, op id recorded.
        assert self._complete(pg_db, row_id, token, 1, op_id="fb-1-1-abc") == 2
        status, attempts, err, *_r, op = self._row(pg_db, row_id)
        assert (status, attempts, err, op) == (2, 1, None, "fb-1-1-abc")
        assert self._row(pg_db, row_id)[7] is True  # completed_at
        # A second completion (replayed / stale) is a no-op.
        assert self._complete(pg_db, row_id, token, 2, "late") == 0
        assert self._row(pg_db, row_id)[0] == 2

    def test_l2_an_expired_lease_is_reclaimed_and_the_old_token_can_no_longer_complete(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-expire")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        old = str(self._mine(self._claim(pg_db), row_id)[7])
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET lease_expires_at = NOW() - INTERVAL '1 s' "
            "WHERE id=%s",
            (row_id,),
        )
        again = self._mine(self._claim(pg_db), row_id)
        assert again[6] == 2, "the reclaim is attempt 2"
        new = str(again[7])
        assert new != old
        assert self._complete(pg_db, row_id, old, 1) == 0, "no double fire"
        assert self._row(pg_db, row_id)[0] == 7
        assert self._complete(pg_db, row_id, new, 1) == 2

    def test_failure_schedules_a_backoff_retry_that_is_claimed_when_due(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-retry")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        token = str(self._mine(self._claim(pg_db), row_id)[7])
        assert self._complete(pg_db, row_id, token, 2, "relay_unconfirmed", "fb-x") == 3
        status, attempts, err, *_r = self._row(pg_db, row_id)
        assert (status, attempts, err) == (3, 1, "relay_unconfirmed")
        assert self._row(pg_db, row_id)[6] is True  # next_retry_at set
        assert self._claim(pg_db) == [], "backoff has not elapsed"
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() - INTERVAL '1 s' "
            "WHERE id=%s",
            (row_id,),
        )
        retried = self._mine(self._claim(pg_db), row_id)
        assert retried[6] == 2
        assert self._complete(pg_db, row_id, str(retried[7]), 1) == 2

    def test_m2_exhausted_attempts_become_dead_and_are_never_claimed_again(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-dead")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        outcome = 0
        for attempt in range(1, 9):
            _q(
                pg_db,
                "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() - INTERVAL '1 s' "
                "WHERE id=%s AND status=3",
                (row_id,),
            )
            mine = self._mine(self._claim(pg_db), row_id)
            assert mine[6] == attempt
            outcome = self._complete(pg_db, row_id, str(mine[7]), 2, "relay_unconfirmed")
        assert outcome == 4, "the 8th failure is DEAD"
        assert self._row(pg_db, row_id)[:3] == (4, 8, "relay_unconfirmed")
        assert self._claim(pg_db) == []
        assert _q(pg_db, "SELECT * FROM reputation_floor_ban_reap(10)", (), _ACTION_LOGIN) == []

    def test_m2_a_crashed_final_attempt_is_reaped_dead_and_returned_for_the_alert(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-reap")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        self._claim(pg_db)
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET attempts = 8, "
            "lease_expires_at = NOW() - INTERVAL '1 s' WHERE id=%s",
            (row_id,),
        )
        assert self._claim(pg_db) == [], "attempts exhausted: not leased again"
        reaped = _q(pg_db, "SELECT * FROM reputation_floor_ban_reap(10)", (), _ACTION_LOGIN)
        assert [(r[0], r[1], r[2]) for r in reaped] == [(row_id, tid, cid)]
        assert self._row(pg_db, row_id)[:3] == (4, 8, "attempts_exhausted")

    def test_a_permanent_refusal_closes_the_row_and_free_text_errors_are_not_stored(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-refuse")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        token = str(self._mine(self._claim(pg_db), row_id)[7])
        assert self._complete(pg_db, row_id, token, 3, "protected_target") == 6
        assert self._row(pg_db, row_id)[:3] == (6, 1, "protected_target")
        assert self._claim(pg_db) == []
        _state, row2 = self._enqueue(pg_db, tid, cid, user, key="fb:evt2")
        token2 = str(self._mine(self._claim(pg_db), row2)[7])
        assert self._complete(pg_db, row2, token2, 2, "User 1234 not found!") == 3
        assert self._row(pg_db, row2)[2] == "unknown"
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            self._complete(pg_db, row2, token2, 9)

    def test_m1_the_breach_is_rechecked_again_right_before_the_command_is_sent(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-recheck")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        token = str(self._mine(self._claim(pg_db), row_id)[7])
        recheck = "SELECT reputation_floor_ban_recheck(%s, %s::uuid)"
        assert _q(pg_db, recheck, (row_id, token), _ACTION_LOGIN)[0][0] == 0
        assert _q(pg_db, recheck, (row_id, str(uuid.uuid4())), _ACTION_LOGIN)[0][0] == 2
        # The score recovers between enqueue and dispatch: the send is cancelled, row refused.
        _q(
            pg_db,
            "UPDATE bundle_reputation_scores SET balance = 900 WHERE community_id=%s",
            (cid,),
        )
        assert _q(pg_db, recheck, (row_id, token), _ACTION_LOGIN)[0][0] == 1
        assert self._row(pg_db, row_id)[:3] == (6, 1, "floor_not_breached")
        assert _q(pg_db, recheck, (row_id, token), _ACTION_LOGIN)[0][0] == 2, (
            "the lease is gone once the row is closed"
        )

    # --- review fixes: quota at claim, kill switch, per-user cap, backlog ------------------

    def _queue_many(
        self, db: PgTestDatabase, tid: int, cid: int, n: int
    ) -> list[int]:
        ids = []
        for i in range(n):
            user = str(uuid.uuid4())
            _score(db, tid, cid, user, 300)
            state, row_id = self._enqueue(db, tid, cid, user, key=f"fb:q{i}")
            assert state == 0
            ids.append(row_id)
        return ids

    def test_the_hourly_quota_is_enforced_at_claim_and_deferred_rows_stay_limited(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, _u = self._slot(pg_db, "t-ob-claimquota")
        ids = self._queue_many(pg_db, tid, cid, 5)
        # The community's quota shrinks after the rows were queued (policy change): the claim,
        # not the enqueue, is now the authority.
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET floor_ban_hourly_quota = 2 "
            "WHERE community_id=%s",
            (cid,),
        )
        claimed = self._claim(pg_db)
        assert len(claimed) == 2, "exactly the quota is leased"
        leased = {r[0] for r in claimed}
        deferred = [i for i in ids if i not in leased]
        rows = _q(
            pg_db,
            "SELECT status, last_error, next_retry_at > NOW() + INTERVAL '29 seconds', "
            "lease_token IS NULL FROM bundle_reputation_floor_bans WHERE id = ANY(%s)",
            (deferred,),
        )
        assert rows == [(5, "rate_limited", True, True)] * 3, (
            "over-quota rows are NOT leased: status 5 with a jittered future retry"
        )
        assert self._claim(pg_db) == [], "nothing more until the window or the retry passes"
        # The retry time passes but the window is still full: still not promoted (a retry or a
        # status-5/3 row can never bypass the quota).
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() - INTERVAL '1 s' "
            "WHERE status = 5 AND community_id=%s",
            (cid,),
        )
        assert self._claim(pg_db) == []
        # One dispatched row leaves the window: exactly ONE slot is free -> exactly one promoted.
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET dispatched_at = NOW() - INTERVAL '2 hours' "
            "WHERE id = %s",
            (claimed[0][0],),
        )
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() - INTERVAL '1 s' "
            "WHERE status = 5 AND community_id=%s",
            (cid,),
        )
        promoted = self._claim(pg_db)
        assert len(promoted) == 1
        assert self._row(pg_db, promoted[0][0])[0] == 7

    def test_a_failed_row_retried_past_its_backoff_cannot_bypass_the_quota(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, _u = self._slot(pg_db, "t-ob-retryquota")
        a, b = self._queue_many(pg_db, tid, cid, 2)
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET floor_ban_hourly_quota = 1 "
            "WHERE community_id=%s",
            (cid,),
        )
        first = self._claim(pg_db)
        assert len(first) == 1
        # Fail it (retry due now): the dispatch still counts inside the window. Its OWN retry reuses
        # that one slot (a row counts once however often it is re-dispatched), but the other
        # queued row can never be leased: the community stays at 1 dispatched row per hour.
        self._complete(pg_db, first[0][0], str(first[0][7]), 2, "relay_unconfirmed")
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() - INTERVAL '1 s' "
            "WHERE status = 3",
        )
        retried = self._claim(pg_db)
        assert [r[0] for r in retried] == [first[0][0]], "only the retry of the same row"
        other = b if first[0][0] == a else a
        assert self._row(pg_db, other)[0] in (1, 5), "the other row stays waiting"
        assert (
            _q(
                pg_db,
                "SELECT COUNT(*) FROM bundle_reputation_floor_bans "
                "WHERE community_id=%s AND dispatched_at IS NOT NULL",
                (cid,),
            )[0][0]
            == 1
        )

    def test_a_concurrent_claim_never_exceeds_the_quota(self, pg_db: PgTestDatabase) -> None:
        tid, cid, _u = self._slot(pg_db, "t-ob-claimconc")
        self._queue_many(pg_db, tid, cid, 8)
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET floor_ban_hourly_quota = 3 "
            "WHERE community_id=%s",
            (cid,),
        )
        got: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            n = len(self._claim(pg_db, limit=8))
            with lock:
                got.append(n)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(got) <= 3, f"leased {sum(got)} across claimants, quota is 3"
        dispatched = _q(
            pg_db,
            "SELECT COUNT(*) FROM bundle_reputation_floor_bans WHERE community_id=%s "
            "AND dispatched_at IS NOT NULL",
            (cid,),
        )[0][0]
        assert dispatched <= 3

    def test_the_durable_kill_switch_stops_enqueue_claim_and_recheck(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-halt")
        _state, row_id = self._enqueue(pg_db, tid, cid, user)
        token = str(self._mine(self._claim(pg_db), row_id)[7])
        _q(pg_db, "SELECT reputation_floor_ban_set_halted(true, 'operator stop')")
        # recheck sees the switch: the leased row is released back to the queue, attempt refunded.
        recheck = "SELECT reputation_floor_ban_recheck(%s, %s::uuid)"
        assert _q(pg_db, recheck, (row_id, token), _ACTION_LOGIN)[0][0] == 3
        assert self._row(pg_db, row_id)[:2] == (1, 0)
        # Nothing is claimable and new enqueues are refused (recorded) while halted.
        assert self._claim(pg_db) == []
        user2 = str(uuid.uuid4())
        _score(pg_db, tid, cid, user2, 300)
        state, rid2 = self._enqueue(pg_db, tid, cid, user2, key="fb:halted")
        assert state == 3
        assert self._row(pg_db, rid2)[:3] == (6, 0, "kill_switch")
        # Cleared: the original row is delivered again, from the start.
        _q(pg_db, "SELECT reputation_floor_ban_set_halted(false, NULL)")
        again = self._mine(self._claim(pg_db), row_id)
        assert again[6] == 1

    def test_a_missing_control_row_reads_as_halted_fail_closed(
        self, pg_db: PgTestDatabase
    ) -> None:
        _q(pg_db, "DELETE FROM bundle_reputation_floor_ban_control")
        assert _q(pg_db, "SELECT reputation_floor_ban_halted()")[0][0] is True
        _q(pg_db, "SELECT reputation_floor_ban_set_halted(false, 'restore')")
        assert _q(pg_db, "SELECT reputation_floor_ban_halted()")[0][0] is False

    def test_one_user_cannot_exhaust_the_community_quota(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-usercap")
        assert self._enqueue(pg_db, tid, cid, user, key="fb:a")[0] == 0
        assert self._enqueue(pg_db, tid, cid, user, key="fb:b")[0] == 0
        state, rid = self._enqueue(pg_db, tid, cid, user, key="fb:c")
        assert (state, rid) == (4, 0), "third distinct event for the same user is capped"
        n = _q(
            pg_db,
            "SELECT COUNT(*) FROM bundle_reputation_floor_bans WHERE user_uuid=%s",
            (user,),
        )[0][0]
        assert n == 2, "a capped enqueue records nothing"
        # A replay of an existing slot is still the idempotent no-op; another user is unaffected.
        assert self._enqueue(pg_db, tid, cid, user, key="fb:a")[0] == 1
        other = str(uuid.uuid4())
        _score(pg_db, tid, cid, other, 300)
        assert self._enqueue(pg_db, tid, cid, other, key="fb:d")[0] == 0

    def test_backlog_reports_waiting_rows_for_the_alert(self, pg_db: PgTestDatabase) -> None:
        tid, cid, _u = self._slot(pg_db, "t-ob-backlog")
        ids = self._queue_many(pg_db, tid, cid, 3)
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET created_at = NOW() - INTERVAL '30 minutes' "
            "WHERE id = %s",
            (ids[0],),
        )
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET status = 5, next_retry_at = NOW() "
            "WHERE id = %s",
            (ids[1],),
        )
        pending, limited, oldest = _q(
            pg_db, "SELECT * FROM reputation_floor_ban_backlog()", (), _ACTION_LOGIN
        )[0]
        assert (pending, limited) == (2, 1)
        assert oldest >= 1800

    # --- input / privilege boundaries -----------------------------------------------------

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"key": "Bad Key"},
            {"key": ""},
            {"key": "x" * 129},
            {"platform": "Discord"},
            {"platform": "d"},
            {"platform": "dis cord"},
            {"scope": 0},
            {"scope": 3},
        ],
    )
    def test_invalid_input_is_22023(
        self, pg_db: PgTestDatabase, kwargs: dict[str, Any]
    ) -> None:
        tid, cid, user = self._slot(pg_db, f"t-ob-inval-{abs(hash(str(kwargs))) % 10**8}")
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            self._enqueue(pg_db, tid, cid, user, **kwargs)

    def test_a_community_outside_the_tenant_is_rejected_42501(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-ob-pair-a")
        other_tid = _tenant(pg_db, "t-ob-pair-b")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            self._enqueue(pg_db, other_tid, cid, user)
        foreign = _community(pg_db, other_tid, "foreign")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            self._enqueue(pg_db, tid, cid, user, origin=foreign)

    def test_each_stage_role_may_execute_only_its_own_functions(
        self, pg_db: PgTestDatabase
    ) -> None:
        sql = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
        process_fns = (
            "reputation_verify_floor_breach(bigint,bigint,uuid,smallint)",
            "reputation_floor_ban_enqueue(bigint,bigint,bigint,uuid,smallint,text,text)",
            "reputation_tenant_communities(bigint)",
        )
        both_fns = ("reputation_floor_ban_halted()",)
        hub_only_fns = ("reputation_floor_ban_set_halted(boolean,text)",)
        action_fns = (
            "reputation_floor_ban_reap(int)",
            "reputation_floor_ban_claim(int,int)",
            "reputation_floor_ban_backlog()",
            "reputation_floor_ban_recheck(bigint,uuid)",
            "reputation_floor_ban_complete(bigint,uuid,smallint,text,text)",
        )
        _q(pg_db, "CREATE ROLE rep_ban_stranger LOGIN PASSWORD %s", (_LOGIN_PW,))
        for fn in process_fns + action_fns + both_fns + hub_only_fns:
            assert _q(pg_db, sql, ("rep_ban_stranger", fn))[0][0] is False, fn
        for fn in both_fns:
            assert _q(pg_db, sql, (_BUNDLE_LOGIN, fn))[0][0] is True, fn
            assert _q(pg_db, sql, (_ACTION_LOGIN, fn))[0][0] is True, fn
        for fn in hub_only_fns:
            assert _q(pg_db, sql, (_BUNDLE_LOGIN, fn))[0][0] is False, fn
            assert _q(pg_db, sql, (_ACTION_LOGIN, fn))[0][0] is False, fn
        for fn in process_fns:
            assert _q(pg_db, sql, (_BUNDLE_LOGIN, fn))[0][0] is True, fn
            assert _q(pg_db, sql, (_ACTION_LOGIN, fn))[0][0] is False, fn
        for fn in action_fns:
            assert _q(pg_db, sql, (_ACTION_LOGIN, fn))[0][0] is True, fn
            assert _q(pg_db, sql, (_BUNDLE_LOGIN, fn))[0][0] is False, fn
        # Neither stage role can read or write the table directly.
        for login in (_BUNDLE_LOGIN, _ACTION_LOGIN):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                _q(pg_db, "SELECT * FROM bundle_reputation_floor_bans", login=login)
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                _q(
                    pg_db,
                    "UPDATE bundle_reputation_floor_bans SET status = 2",
                    login=login,
                )

    def test_tenant_communities_is_the_tenants_live_communities_only(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-ob-fanout")
        a = _community(pg_db, tid, "a")
        b = _community(pg_db, tid, "b")
        gone = _community(pg_db, tid, "gone")
        inactive = _community(pg_db, tid, "inactive")
        _q(pg_db, "UPDATE communities SET deleted_at = NOW() WHERE id=%s", (gone,))
        _q(pg_db, "UPDATE communities SET is_active = false WHERE id=%s", (inactive,))
        _community(pg_db, _tenant(pg_db, "t-ob-fanout-other"), "other-tenant")
        got = _q(
            pg_db,
            "SELECT * FROM reputation_tenant_communities(%s)",
            (tid,),
            _BUNDLE_LOGIN,
        )
        assert [r[0] for r in got] == [a, b]
