"""Tests for 0098_reputation_floor_ban against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head, `reputation_verify_floor_breach()` (community hard and tenant
scopes, switch off, floor boundaries, unscored default, tenant pairing, fail-closed inputs), the
audit/idempotency/rate-limit/durable-retry functions, the tenant fan-out set, and the bundle
role's privilege boundary (EXECUTE on the functions, no direct table access).
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
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0098_reputation_floor_ban.py"
_BUNDLE_LOGIN = "rep_ban_bundle"
_LOGIN_PW = "rep-ban-test-only-pw"
_VERIFY = "SELECT reputation_verify_floor_breach(%s, %s, %s::uuid, %s::smallint)"
_BEGIN = "SELECT reputation_floor_ban_begin(%s, %s, %s, %s::uuid, %s::smallint, %s, %s)"
_RECORD = "SELECT reputation_floor_ban_record(%s, %s, %s::uuid, %s::smallint, %s, %s::smallint, %s)"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0098", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_0097(self) -> None:
        m = _load()
        assert m.revision == "0098_reputation_floor_ban"
        assert len(m.revision) <= 32
        assert m.down_revision == "0097_reputation_unify"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        assert ScriptDirectory.from_config(cfg).get_heads() == [
            "0098_reputation_floor_ban"
        ]


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0098-floor-ban") as db:
        with _connect(db) as conn, conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
            )
            cur.execute(
                f"CREATE ROLE {_BUNDLE_LOGIN} LOGIN PASSWORD %s IN ROLE waddles_bundle_reputation",
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

    def test_the_bundle_role_may_execute_it_but_not_read_the_audit_table(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-vf-role")
        cid = _community(pg_db, tid, "c1")
        assert _verify(pg_db, tid, cid, str(uuid.uuid4()), 1, _BUNDLE_LOGIN) is False
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _q(pg_db, "SELECT * FROM bundle_reputation_floor_bans", login=_BUNDLE_LOGIN)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _q(
                pg_db,
                "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, "
                "origin_community_id, user_uuid, scope, idempotency_key, reason, platform) "
                "VALUES (1, 1, 1, gen_random_uuid(), 1, 'k', 'community_hard_floor', 'discord')",
                login=_BUNDLE_LOGIN,
            )
        # PUBLIC has no EXECUTE: a plain role cannot call it.
        _q(pg_db, "CREATE ROLE rep_ban_stranger LOGIN PASSWORD %s", (_LOGIN_PW,))
        for fn in (
            "reputation_verify_floor_breach(bigint,bigint,uuid,smallint)",
            "reputation_floor_ban_begin(bigint,bigint,bigint,uuid,smallint,text,text)",
            "reputation_floor_ban_record(bigint,bigint,uuid,smallint,text,smallint,text)",
            "reputation_floor_ban_due(bigint,int)",
            "reputation_tenant_communities(bigint)",
        ):
            sql = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
            assert _q(pg_db, sql, ("rep_ban_stranger", fn))[0][0] is False, fn
            assert _q(pg_db, sql, (_BUNDLE_LOGIN, fn))[0][0] is True, fn


@requires_docker
class TestFloorBanLifecycle:
    def _slot(self, db: PgTestDatabase, name: str) -> tuple[int, int, str]:
        tid = _tenant(db, name)
        return tid, _community(db, tid, "c1"), str(uuid.uuid4())

    def _begin(
        self,
        db: PgTestDatabase,
        tid: int,
        cid: int,
        user: str,
        *,
        scope: int = 1,
        key: str = "evt1:hard",
        platform: str = "discord",
        origin: int | None = None,
        login: str | None = _BUNDLE_LOGIN,
    ) -> int:
        return _q(
            db,
            _BEGIN,
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
        )[0][0]

    def _record(
        self,
        db: PgTestDatabase,
        tid: int,
        cid: int,
        user: str,
        outcome: int,
        error: str = "",
        *,
        scope: int = 1,
        key: str = "evt1:hard",
    ) -> None:
        _q(db, _RECORD, (tid, cid, user, scope, key, outcome, error), _BUNDLE_LOGIN)

    def _row(self, db: PgTestDatabase, cid: int) -> tuple[Any, ...]:
        return _q(
            db,
            "SELECT status, attempts, last_error, actor, reason, platform, "
            "next_retry_at IS NOT NULL, completed_at IS NOT NULL "
            "FROM bundle_reputation_floor_bans WHERE community_id=%s",
            (cid,),
        )[0]

    def test_audit_row_carries_actor_reason_scope_platform_and_outcome(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-audit")
        assert self._begin(pg_db, tid, cid, user) == 0
        assert self._row(pg_db, cid)[0] == 1  # pending
        self._record(pg_db, tid, cid, user, 1)
        status, attempts, err, actor, reason, platform, retry, completed = self._row(
            pg_db, cid
        )
        assert (status, attempts, err) == (2, 1, None)
        assert (actor, reason, platform) == (
            "system:reputation_floor",
            "community_hard_floor",
            "discord",
        )
        assert (retry, completed) == (False, True)

    def test_replay_of_a_done_ban_is_idempotent(self, pg_db: PgTestDatabase) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-idem")
        assert self._begin(pg_db, tid, cid, user) == 0
        self._record(pg_db, tid, cid, user, 1)
        assert self._begin(pg_db, tid, cid, user) == 1  # already done: do nothing
        assert self._begin(pg_db, tid, cid, user) == 1
        assert self._row(pg_db, cid)[:2] == (2, 1), (
            "a replay never re-opens or recounts"
        )
        # A different key / scope / user is a distinct slot.
        assert self._begin(pg_db, tid, cid, user, key="evt2:hard") == 0
        assert self._begin(pg_db, tid, cid, user, scope=2, key="evt1:hard") == 0
        assert self._begin(pg_db, tid, cid, str(uuid.uuid4())) == 0

    def test_failure_schedules_a_durable_retry_that_due_leases_once(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-retry")
        origin = _community(pg_db, tid, "origin")
        assert self._begin(pg_db, tid, cid, user, scope=2, origin=origin) == 0
        self._record(pg_db, tid, cid, user, 2, "not_granted", scope=2)
        status, attempts, err, *_rest, retry, _done = self._row(pg_db, cid)
        assert (status, attempts, err, retry) == (3, 1, "not_granted", True)
        # Backoff has not elapsed: nothing due yet.
        assert (
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_due(%s, 10)",
                (tid,),
                _BUNDLE_LOGIN,
            )
            == []
        )
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() - INTERVAL '1 second' "
            "WHERE community_id=%s",
            (cid,),
        )
        due = _q(
            pg_db,
            "SELECT * FROM reputation_floor_ban_due(%s, 10)",
            (tid,),
            _BUNDLE_LOGIN,
        )
        assert due == [(cid, origin, user, 2, "evt1:hard", "discord")]
        # Leased: a second claim (another replica) gets nothing.
        assert (
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_due(%s, 10)",
                (tid,),
                _BUNDLE_LOGIN,
            )
            == []
        )
        # The retry re-begins the same slot (attempts +1) and can then complete.
        assert self._begin(pg_db, tid, cid, user, scope=2, origin=origin) == 0
        self._record(pg_db, tid, cid, user, 1, scope=2)
        assert self._row(pg_db, cid)[:2] == (2, 2)

    def test_exhausted_retries_stay_visible_and_are_never_due(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-exhaust")
        for _ in range(8):
            assert self._begin(pg_db, tid, cid, user) == 0
            self._record(pg_db, tid, cid, user, 2, "target_unavailable")
        status, attempts, err, *_rest, retry, _done = self._row(pg_db, cid)
        assert (status, attempts, err, retry) == (3, 8, "target_unavailable", False)
        assert (
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_due(%s, 10)",
                (tid,),
                _BUNDLE_LOGIN,
            )
            == []
        )

    def test_refused_outcome_is_recorded_and_free_text_errors_are_not_stored(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-refused")
        assert self._begin(pg_db, tid, cid, user) == 0
        self._record(pg_db, tid, cid, user, 3, "floor_not_breached")
        assert self._row(pg_db, cid)[:3] == (4, 1, "floor_not_breached")
        assert self._begin(pg_db, tid, cid, user, key="evt2:hard") == 0
        self._record(pg_db, tid, cid, user, 2, "User 1234 not found!", key="evt2:hard")
        err = _q(
            pg_db,
            "SELECT last_error FROM bundle_reputation_floor_bans "
            "WHERE community_id=%s AND idempotency_key='evt2:hard'",
            (cid,),
        )[0][0]
        assert err == "unknown"

    def test_only_a_pending_attempt_can_be_recorded(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-pending")
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            self._record(pg_db, tid, cid, user, 1)  # never begun
        assert self._begin(pg_db, tid, cid, user) == 0
        self._record(pg_db, tid, cid, user, 1)
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            self._record(pg_db, tid, cid, user, 2, "late")  # already done
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            self._record(pg_db, tid, cid, user, 9)

    def test_per_community_rate_limit_throttles_and_records(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, _user = self._slot(pg_db, "t-fb-rate")
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, "
            "floor_ban_hourly_quota) VALUES (%s, %s, 3)",
            (tid, cid),
        )
        for i in range(3):
            assert self._begin(pg_db, tid, cid, str(uuid.uuid4()), key=f"k{i}") == 0
        victim = str(uuid.uuid4())
        assert self._begin(pg_db, tid, cid, victim, key="k9") == 2
        rows = _q(
            pg_db,
            "SELECT status, last_error FROM bundle_reputation_floor_bans "
            "WHERE community_id=%s AND idempotency_key='k9'",
            (cid,),
        )
        assert rows == [(5, "rate_limited")]
        # Another community is unaffected.
        other = _community(pg_db, tid, "c-other")
        assert self._begin(pg_db, tid, other, victim, key="k9") == 0

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
        tid, cid, user = self._slot(
            pg_db, f"t-fb-inval-{abs(hash(str(kwargs))) % 10**8}"
        )
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            self._begin(pg_db, tid, cid, user, **kwargs)

    def test_a_community_outside_the_tenant_is_rejected_42501(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = self._slot(pg_db, "t-fb-pair-a")
        other_tid = _tenant(pg_db, "t-fb-pair-b")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            self._begin(pg_db, other_tid, cid, user)
        foreign = _community(pg_db, other_tid, "foreign")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            self._begin(pg_db, tid, cid, user, origin=foreign)
        # Retry rows of another tenant are never returned.
        assert (
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_due(%s, 10)",
                (other_tid,),
                _BUNDLE_LOGIN,
            )
            == []
        )

    def test_tenant_communities_is_the_tenants_live_communities_only(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid = _tenant(pg_db, "t-fb-fanout")
        a = _community(pg_db, tid, "a")
        b = _community(pg_db, tid, "b")
        gone = _community(pg_db, tid, "gone")
        inactive = _community(pg_db, tid, "inactive")
        _q(pg_db, "UPDATE communities SET deleted_at = NOW() WHERE id=%s", (gone,))
        _q(pg_db, "UPDATE communities SET is_active = false WHERE id=%s", (inactive,))
        _community(pg_db, _tenant(pg_db, "t-fb-fanout-other"), "other-tenant")
        got = _q(
            pg_db,
            "SELECT * FROM reputation_tenant_communities(%s)",
            (tid,),
            _BUNDLE_LOGIN,
        )
        assert [r[0] for r in got] == [a, b]
