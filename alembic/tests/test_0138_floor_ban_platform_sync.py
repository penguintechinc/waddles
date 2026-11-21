"""Tests for 0138_floor_ban_platform_sync against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head; a Waddles lift enqueuing exactly ONE platform unban for a
delivered ban (idempotent, none for an undelivered one); unban rows never denying access and never
throttled/cancelled; claim/recheck/complete over the same outbox (retry with backoff, then DEAD);
a platform-originated lift that lifts without echoing an unban, clears the soft ban, stays sticky,
and cannot cross tenants or communities; and the downgrade / re-upgrade round trip.
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
_PATH = _ROOT / "alembic" / "versions" / "0138_floor_ban_platform_sync.py"
_LIFT = "SELECT reputation_floor_ban_lift(%s, %s::uuid[], %s::uuid)"
_PLATFORM_LIFT = (
    "SELECT r_lifted, r_soft_cleared FROM reputation_floor_ban_lift_from_platform("
    "%s, %s, %s, %s::uuid, 'penguintechinc.waddles.chatguard', 4102444800000)"
)
_ENQUEUE = (
    "SELECT r_state, r_id FROM reputation_floor_ban_enqueue("
    "%s, %s, %s, %s::uuid, %s::smallint, %s, 'discord')"
)
_CLAIM = (
    "SELECT r_id, r_user_uuid, r_attempt, r_lease_token, r_action "
    "FROM reputation_floor_ban_claim(50, 120)"
)
_SYNC_ACTOR = "00000000-0000-4000-8000-00000000f5c0"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0138", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_0137(self) -> None:
        m = _load()
        assert m.revision == "0138_floor_ban_platform_sync"
        assert len(m.revision) <= 32
        assert m.down_revision == "0137_floor_ban_lift"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f"diverged alembic heads: {heads}"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0138-floor-ban-sync") as db:
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


def _setup(db: PgTestDatabase, slug: str) -> tuple[int, int]:
    tid = _q(
        db,
        "INSERT INTO tenants (slug) VALUES (%s) ON CONFLICT (slug) DO UPDATE "
        "SET slug = EXCLUDED.slug RETURNING id",
        (slug,),
    )[0][0]
    cid = _q(
        db,
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (slug, tid),
    )[0][0]
    return tid, cid


def _row(db: PgTestDatabase, tid: int, cid: int, user: str, status: int) -> int:
    return _q(
        db,
        "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, origin_community_id, "
        "user_uuid, scope, idempotency_key, reason, platform, status) "
        "VALUES (%s, %s, %s, %s, 1, %s, 'community_hard_floor', 'discord', %s) RETURNING id",
        (tid, cid, cid, user, f"k-{uuid.uuid4().hex[:12]}", status),
    )[0][0]


def _status(db: PgTestDatabase, row_id: int) -> int:
    return _q(
        db, "SELECT status FROM bundle_reputation_floor_bans WHERE id = %s", (row_id,)
    )[0][0]


def _unbans(db: PgTestDatabase, tid: int, user: str) -> list[Any]:
    return _q(
        db,
        "SELECT id, status, source_ban_id, idempotency_key, platform FROM "
        "bundle_reputation_floor_bans WHERE tenant_id = %s AND user_uuid = %s::uuid "
        "AND action = 2",
        (tid, user),
    )


def _denying(db: PgTestDatabase, tid: int, user: str) -> int:
    """Rows that deny access: the hub's `FLOOR_BAN_ACTIVE_STATUSES` over BAN rows only."""
    return _q(
        db,
        "SELECT count(*) FROM bundle_reputation_floor_bans WHERE tenant_id = %s "
        "AND user_uuid = %s::uuid AND action = 1 AND status IN (1, 2, 3, 4, 5, 7)",
        (tid, user),
    )[0][0]


@requires_docker
class TestWaddlesLiftEnqueuesOneUnban:
    @pytest.mark.parametrize("status", (2, 7))
    def test_a_delivered_ban_gets_exactly_one_unban_and_a_second_lift_adds_none(
        self, pg_db: PgTestDatabase, status: int
    ) -> None:
        tid, cid = _setup(pg_db, f"t-unban-{status}")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, status)
        assert _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))[0][0] == 1
        unbans = _unbans(pg_db, tid, user)
        assert len(unbans) == 1
        _, ustatus, source, key, platform = unbans[0]
        assert (ustatus, source, key, platform) == (1, rid, f"unban:{rid}", "discord")
        assert _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))[0][0] == 0
        assert len(_unbans(pg_db, tid, user)) == 1, (
            "idempotent: still exactly one unban"
        )
        assert _status(pg_db, rid) == 8

    @pytest.mark.parametrize("status", (1, 3, 4, 5))
    def test_an_undelivered_ban_enqueues_no_unban(
        self, pg_db: PgTestDatabase, status: int
    ) -> None:
        tid, cid = _setup(pg_db, f"t-nounban-{status}")
        user = str(uuid.uuid4())
        _row(pg_db, tid, cid, user, status)
        assert _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))[0][0] == 1
        assert _unbans(pg_db, tid, user) == []

    def test_unban_rows_never_deny_and_are_not_lifted_by_a_second_lift(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-unban-nodeny")
        user = str(uuid.uuid4())
        _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _unbans(pg_db, tid, user)[0][1] == 1  # a queued unban row exists...
        assert _denying(pg_db, tid, user) == 0  # ...and denies nobody


@requires_docker
class TestUnbanRidesTheSameOutbox:
    def test_claim_returns_the_action_ignores_the_ban_quota_and_recheck_never_cancels_it(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-unban-claim")
        user, other = str(uuid.uuid4()), str(uuid.uuid4())
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled, "
            "hard_ban_floor, floor_ban_hourly_quota) VALUES (%s, %s, true, 400, 1)",
            (tid, cid),
        )
        # The hourly BAN quota (1) is already used by another user's dispatched ban.
        used = _row(pg_db, tid, cid, other, 2)
        _q(
            pg_db,
            "UPDATE bundle_reputation_floor_bans SET dispatched_at = NOW() WHERE id = %s",
            (used,),
        )
        rid = _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        claimed = {r[0]: r for r in _q(pg_db, _CLAIM)}
        unban_id = _unbans(pg_db, tid, user)[0][0]
        assert unban_id in claimed, "the ban quota must not defer an unban"
        assert claimed[unban_id][4] == 2
        assert rid not in claimed, "the lifted ban row itself is never re-dispatched"
        lease = claimed[unban_id][3]
        # The score is NOT below the floor (no score row -> default 600 >= 400): a ban would be
        # cancelled by the re-check, an unban must go.
        assert (
            _q(
                pg_db,
                "SELECT reputation_floor_ban_recheck(%s, %s::uuid)",
                (unban_id, lease),
            )[0][0]
            == 0
        )

    def test_a_failing_unban_retries_with_backoff_then_is_dead_after_8_attempts(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-unban-dead")
        user = str(uuid.uuid4())
        _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        unban_id = _unbans(pg_db, tid, user)[0][0]
        for attempt in range(1, 9):
            claimed = {r[0]: r for r in _q(pg_db, _CLAIM)}
            assert unban_id in claimed, f"attempt {attempt} must be claimable"
            assert claimed[unban_id][2] == attempt
            status = _q(
                pg_db,
                "SELECT reputation_floor_ban_complete(%s, %s::uuid, 2::smallint, "
                "'relay_unconfirmed', NULL)",
                (unban_id, claimed[unban_id][3]),
            )[0][0]
            if attempt < 8:
                assert status == 3, "retry scheduled"
                assert _q(
                    pg_db,
                    "SELECT next_retry_at > NOW() FROM bundle_reputation_floor_bans WHERE id = %s",
                    (unban_id,),
                )[0][0], "with backoff"
                _q(
                    pg_db,
                    "UPDATE bundle_reputation_floor_bans SET next_retry_at = NOW() WHERE id = %s",
                    (unban_id,),
                )
            else:
                assert status == 4, (
                    "attempts exhausted: DEAD (the dispatcher raises the alert)"
                )
        assert _status(pg_db, unban_id) == 4
        assert _unbans(pg_db, tid, user)[0][1] == 4

    def test_a_confirmed_unban_completes_done(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _setup(pg_db, "t-unban-done")
        user = str(uuid.uuid4())
        _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        unban_id = _unbans(pg_db, tid, user)[0][0]
        claimed = {r[0]: r for r in _q(pg_db, _CLAIM)}
        done = _q(
            pg_db,
            "SELECT reputation_floor_ban_complete(%s, %s::uuid, 1::smallint, NULL, 'fu-1-1-x')",
            (unban_id, claimed[unban_id][3]),
        )[0][0]
        assert done == 2
        assert _denying(pg_db, tid, user) == 0, "a done unban row still denies nobody"


@requires_docker
class TestPlatformUnbanLiftsWithoutEcho:
    def test_lifts_the_ban_clears_the_soft_ban_and_sends_no_unban_back(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-lift")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 2)
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_soft_bans (tenant_id, community_id, user_uuid, "
            "reason_code, set_by) VALUES (%s, %s, %s, 1, 'system:floor')",
            (tid, cid, user),
        )
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", user))[0] == (1, 1)
        assert _status(pg_db, rid) == 8
        lifted_by, origin = _q(
            pg_db,
            "SELECT lifted_by::text, lift_origin FROM bundle_reputation_floor_bans WHERE id = %s",
            (rid,),
        )[0]
        assert (lifted_by, origin) == (_SYNC_ACTOR, 2)
        assert _unbans(pg_db, tid, user) == [], (
            "a sync-originated lift must not echo an unban"
        )
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_soft_bans WHERE community_id = %s",
                (cid,),
            )[0][0]
            == 0
        )
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_adjustments WHERE community_id = %s "
                "AND kind = 'soft_ban_clear' AND actor = 'system:platform_sync'",
                (cid,),
            )[0][0]
            == 1
        )
        # Idempotent: replaying the same platform event changes nothing.
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", user))[0] == (0, 0)

    def test_the_platform_lift_is_sticky_like_an_admin_lift(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-sticky")
        user = str(uuid.uuid4())
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled, "
            "hard_ban_floor) VALUES (%s, %s, true, 400)",
            (tid, cid),
        )
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
            "VALUES (%s, %s, %s, 300)",
            (tid, cid, user),
        )
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))[0][0] == 0
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", user))[0][0] == 1
        assert _denying(pg_db, tid, user) == 0
        # Still below the floor + a NEW event key: refused, the platform lift stays lifted.
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        assert _denying(pg_db, tid, user) == 0

    def test_a_cross_tenant_event_cannot_lift_another_tenants_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid_a, cid_a = _setup(pg_db, "t-plat-a")
        tid_b, cid_b = _setup(pg_db, "t-plat-b")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid_a, cid_a, user, 2)
        # Tenant B's own (valid) community + the same user token + the same platform.
        assert _q(pg_db, _PLATFORM_LIFT, (tid_b, cid_b, "discord", user))[0] == (0, 0)
        assert _status(pg_db, rid) == 2, "tenant A's ban is untouched"
        assert _denying(pg_db, tid_a, user) == 1
        # Naming tenant A's community under tenant B is refused outright.
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _q(pg_db, _PLATFORM_LIFT, (tid_b, cid_a, "discord", user))
        assert _status(pg_db, rid) == 2

    def test_a_community_event_lifts_only_that_communitys_rows_and_platform(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-scope")
        cid2 = _q(
            pg_db,
            "INSERT INTO communities (name, tenant_id) VALUES ('t-plat-scope-2', %s) RETURNING id",
            (tid,),
        )[0][0]
        user = str(uuid.uuid4())
        mine = _row(pg_db, tid, cid, user, 2)
        elsewhere = _row(pg_db, tid, cid2, user, 2)
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "twitch", user))[0] == (0, 0)
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", user))[0] == (1, 0)
        assert (_status(pg_db, mine), _status(pg_db, elsewhere)) == (8, 2)

    @pytest.mark.parametrize("platform", ("", "Discord", "x" * 40))
    def test_invalid_input_fails_closed(
        self, pg_db: PgTestDatabase, platform: str
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-invalid")
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _q(pg_db, _PLATFORM_LIFT, (tid, cid, platform, str(uuid.uuid4())))


def _floor_user(
    db: PgTestDatabase, slug: str, balance: int, *, drop: int | None = None
) -> Any:
    """A community with a hard floor of 400 and one scored user; returns (tid, cid, user)."""
    tid, cid = _setup(db, slug)
    user = str(uuid.uuid4())
    extra = "" if drop is None else f", lift_rearm_drop = {int(drop)}"
    _q(
        db,
        "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled, "
        "hard_ban_floor) VALUES (%s, %s, true, 400)",
        (tid, cid),
    )
    if extra:
        _q(
            db,
            "UPDATE bundle_reputation_policy SET "
            + extra[2:]
            + " WHERE community_id = %s",
            (cid,),
        )
    _q(
        db,
        "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
        "VALUES (%s, %s, %s, %s)",
        (tid, cid, user, balance),
    )
    return tid, cid, user


def _balance(db: PgTestDatabase, cid: int, user: str, balance: int) -> None:
    _q(
        db,
        "UPDATE bundle_reputation_scores SET balance = %s WHERE community_id = %s "
        "AND user_uuid = %s::uuid",
        (balance, cid, user),
    )


@requires_docker
class TestRearmOnANewLow:
    """A lift forgives past behaviour, not a NEW offence (score_at_lift - lift_rearm_drop)."""

    def test_the_score_at_lift_is_recorded_atomically_by_both_lift_paths(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-snap", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        snap = _q(
            pg_db,
            "SELECT score_at_lift FROM bundle_reputation_floor_bans WHERE tenant_id = %s",
            (tid,),
        )
        assert snap == [(380,)]
        tid2, cid2, user2 = _floor_user(pg_db, "t-rearm-snap2", 360)
        _row(pg_db, tid2, cid2, user2, 2)
        _q(pg_db, _PLATFORM_LIFT, (tid2, cid2, "discord", user2))
        assert _q(
            pg_db,
            "SELECT score_at_lift FROM bundle_reputation_floor_bans WHERE tenant_id = %s",
            (tid2,),
        ) == [(360,)]

    def test_same_score_stays_lifted(self, pg_db: PgTestDatabase) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-same", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        assert _denying(pg_db, tid, user) == 0

    def test_a_drop_of_at_least_the_threshold_bans_again(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-drop", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        _balance(pg_db, cid, user, 375)  # exactly the default threshold (5) below 380
        state, rid = _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0]
        assert state == 0 and _status(pg_db, rid) == 1
        assert _denying(pg_db, tid, user) == 1

    def test_a_tiny_drop_below_the_threshold_stays_lifted(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-tiny", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        _balance(pg_db, cid, user, 376)  # 4 < 5
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        assert _denying(pg_db, tid, user) == 0

    def test_the_threshold_is_policy_driven(self, pg_db: PgTestDatabase) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-policy", 380, drop=20)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        _balance(pg_db, cid, user, 365)  # 15 < 20
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        _balance(pg_db, cid, user, 360)  # 20 >= 20
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt3"))[0][0] == 0

    def test_a_new_low_above_the_floor_still_needs_a_breach(
        self, pg_db: PgTestDatabase
    ) -> None:
        """Re-arming only lifts the stickiness; the floor itself must still be breached."""
        tid, cid, user = _floor_user(pg_db, "t-rearm-nobreach", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _balance(
            pg_db, cid, user, 450
        )  # recovered before the lift -> recovered_at at lift
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        _balance(pg_db, cid, user, 420)  # a drop, but still above the 400 floor
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        assert _denying(pg_db, tid, user) == 0

    def test_recover_then_cross_still_bans(self, pg_db: PgTestDatabase) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-recross", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        _balance(pg_db, cid, user, 450)  # recovers
        _balance(
            pg_db, cid, user, 379
        )  # falls below the floor again (a tiny drop vs the lift)
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt3"))[0][0] == 0
        assert _denying(pg_db, tid, user) == 1

    def test_only_the_most_recent_lift_governs(self, pg_db: PgTestDatabase) -> None:
        tid, cid, user = _floor_user(pg_db, "t-rearm-latest", 380)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        _balance(pg_db, cid, user, 370)  # new low -> banned again
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 0
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))  # lifted again, snapshot 370
        _balance(
            pg_db, cid, user, 372
        )  # above the NEW snapshot: the old lift must not re-arm
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt3"))[0][0] == 3
        assert _denying(pg_db, tid, user) == 0

    def test_tenant_scope_rearms_on_a_new_low_too(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _setup(pg_db, "t-rearm-tenant")
        user = str(uuid.uuid4())
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_tenant_policy (tenant_id, tenant_auto_ban_enabled, "
            "tenant_auto_ban_floor) VALUES (%s, true, 400)",
            (tid,),
        )
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_tenant_scores (tenant_id, user_uuid, score) "
            "VALUES (%s, %s, 380)",
            (tid, user),
        )
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 2, "evt1"))[0][0] == 0
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 2, "evt2"))[0][0] == 3
        _q(
            pg_db,
            "UPDATE bundle_reputation_tenant_scores SET score = 370 WHERE tenant_id = %s",
            (tid,),
        )
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 2, "evt3"))[0][0] == 0


def _row_scope(
    db: PgTestDatabase, tid: int, cid: int, user: str, status: int, scope: int
) -> int:
    return _q(
        db,
        "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, origin_community_id, "
        "user_uuid, scope, idempotency_key, reason, platform, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'discord', %s) RETURNING id",
        (
            tid,
            cid,
            cid,
            user,
            scope,
            f"k-{uuid.uuid4().hex[:12]}",
            "community_hard_floor" if scope == 1 else "tenant_floor",
            status,
        ),
    )[0][0]


@requires_docker
class TestPlatformLiftHardening:
    def test_a_community_platform_unban_never_lifts_a_tenant_scope_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-scope2")
        user = str(uuid.uuid4())
        community_ban = _row_scope(pg_db, tid, cid, user, 2, 1)
        tenant_ban = _row_scope(pg_db, tid, cid, user, 2, 2)
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", user))[0][0] == 1
        assert (_status(pg_db, community_ban), _status(pg_db, tenant_ban)) == (8, 2)
        assert _denying(pg_db, tid, user) == 1, "the tenant-scope ban still denies"
        # Tenant-scope bans lift only through the tenant-admin path.
        assert _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))[0][0] == 1
        assert _status(pg_db, tenant_ban) == 8

    def test_the_calling_app_is_recorded_on_the_ban_and_the_ledger(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-app")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 2)
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_soft_bans (tenant_id, community_id, user_uuid, "
            "reason_code, set_by) VALUES (%s, %s, %s, 1, 'system:floor')",
            (tid, cid, user),
        )
        _q(
            pg_db,
            "SELECT * FROM reputation_floor_ban_lift_from_platform(%s, %s, 'discord', %s::uuid, "
            "'penguintechinc.waddles.chatguard', 4102444800000)",
            (tid, cid, user),
        )
        assert (
            _q(
                pg_db,
                "SELECT lift_source_app FROM bundle_reputation_floor_bans WHERE id = %s",
                (rid,),
            )[0][0]
            == "penguintechinc.waddles.chatguard"
        )
        assert _q(
            pg_db,
            "SELECT source_event FROM bundle_reputation_adjustments WHERE community_id = %s "
            "AND kind = 'soft_ban_clear'",
            (cid,),
        ) == [("soft_ban.platform_unban:penguintechinc.waddles.chatguard",)]

    def test_a_manual_soft_ban_and_an_unrelated_soft_ban_are_never_cleared(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-soft")
        manual, floor_only = str(uuid.uuid4()), str(uuid.uuid4())
        for user, reason in ((manual, 2), (floor_only, 1)):
            _q(
                pg_db,
                "INSERT INTO bundle_reputation_soft_bans (tenant_id, community_id, user_uuid, "
                "reason_code, set_by) VALUES (%s, %s, %s, %s, 'x')",
                (tid, cid, user, reason),
            )
        # A user with a delivered ban AND a MANUAL soft ban: only the ban is lifted.
        _row(pg_db, tid, cid, manual, 2)
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", manual))[0] == (1, 0)
        # A user with a floor soft ban but NO ban row for this platform: nothing is cleared.
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", floor_only))[0] == (0, 0)
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_soft_bans WHERE community_id = %s",
                (cid,),
            )[0][0]
            == 2
        )

    def test_a_call_without_the_calling_app_fails_and_no_stale_overload_exists(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-plat-noapp")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 2)
        with pytest.raises(
            psycopg2.errors.UndefinedFunction
        ):  # the old 4-argument overload
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_lift_from_platform(%s, %s, 'discord', %s::uuid)",
                (tid, cid, user),
            )
        with pytest.raises(
            psycopg2.errors.InvalidParameterValue
        ):  # NULL app: no default
            _q(
                pg_db,
                "SELECT * FROM reputation_floor_ban_lift_from_platform("
                "%s, %s, 'discord', %s::uuid, NULL, 4102444800000)",
                (tid, cid, user),
            )
        assert _status(pg_db, rid) == 2

    @pytest.mark.parametrize("status", (1, 3, 4, 5, 7))
    def test_a_platform_unban_also_cancels_undelivered_bans(
        self, pg_db: PgTestDatabase, status: int
    ) -> None:
        tid, cid = _setup(pg_db, f"t-plat-cancel-{status}")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, status)
        assert _q(pg_db, _PLATFORM_LIFT, (tid, cid, "discord", user))[0][0] == 1
        assert _status(pg_db, rid) == 8
        assert _denying(pg_db, tid, user) == 0
        claimed = {r[0] for r in _q(pg_db, _CLAIM)}
        assert rid not in claimed, "a cancelled ban is never sent"

    def test_the_unban_namespace_cannot_collide_with_ban_keys(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-ns")
        user = str(uuid.uuid4())
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "unban:7"))
        with pytest.raises(psycopg2.errors.CheckViolation):
            _q(
                pg_db,
                "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, "
                "origin_community_id, user_uuid, scope, idempotency_key, reason, platform, status) "
                "VALUES (%s, %s, %s, %s, 1, 'unban:9', 'community_hard_floor', 'discord', 2)",
                (tid, cid, cid, user),
            )
        # A ban key and an unban key with the same text live in different namespaces.
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, "
            "origin_community_id, user_uuid, scope, idempotency_key, reason, platform, status, "
            "action) VALUES (%s, %s, %s, %s, 1, 'same', 'community_hard_floor', 'discord', 2, 1), "
            "(%s, %s, %s, %s, 1, 'same', 'community_hard_floor', 'discord', 1, 2)",
            (tid, cid, cid, user, tid, cid, cid, user),
        )

    def test_a_shortfall_of_unbans_fails_the_lift_loudly_and_rolls_it_back(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-shortfall")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 2)
        # Something already occupies this ban's unban slot: the INSERT would silently do nothing.
        _q(
            pg_db,
            "INSERT INTO bundle_reputation_floor_bans (tenant_id, community_id, "
            "origin_community_id, user_uuid, scope, idempotency_key, reason, platform, status, "
            "action) VALUES (%s, %s, %s, %s, 1, %s, 'community_hard_floor', 'discord', 6, 2)",
            (tid, cid, cid, user, f"unban:{rid}"),
        )
        with pytest.raises(psycopg2.errors.DataCorrupted, match="unban_shortfall"):
            _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _status(pg_db, rid) == 2, (
            "the whole lift rolled back: the ban still stands"
        )


@requires_docker
class TestErasureOfUnbanRows:
    def test_a_done_unban_row_is_pseudonymised_without_a_suppression_code(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-unban-erase")
        user = str(uuid.uuid4())
        _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        unban_id = _unbans(pg_db, tid, user)[0][0]
        claimed = {r[0]: r for r in _q(pg_db, _CLAIM)}
        _q(
            pg_db,
            "SELECT reputation_floor_ban_complete(%s, %s::uuid, 1::smallint, NULL, NULL)",
            (unban_id, claimed[unban_id][3]),
        )
        deleted, pseudonymised, pending = _q(
            pg_db,
            "SELECT r_deleted, r_pseudonymised, r_pending FROM "
            "reputation_floor_ban_erase_subject(%s::uuid, %s, 'discord', NULL)",
            (user, tid),
        )[0]
        assert (deleted, pseudonymised, pending) == (0, 2, 0), "lifted ban + done unban"


@requires_docker
class TestRoundTrip:
    def test_downgrade_one_step_then_reupgrade(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _setup(pg_db, "t-sync-roundtrip")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert len(_unbans(pg_db, tid, user)) == 1
        alembic_cli("downgrade", "0137_floor_ban_lift", dsn=pg_db.dsn)
        cols = _q(
            pg_db,
            "SELECT column_name FROM information_schema.columns WHERE table_name = "
            "'bundle_reputation_floor_bans' AND column_name IN "
            "('action', 'source_ban_id', 'lift_origin', 'score_at_lift')",
        )
        assert cols == []
        assert _status(pg_db, rid) == 8, "the 0137 lift state survives a 0138 downgrade"
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
        claimed = _q(pg_db, "SELECT r_action FROM reputation_floor_ban_claim(1, 120)")
        assert isinstance(claimed, list), "claim works again after the re-upgrade"
