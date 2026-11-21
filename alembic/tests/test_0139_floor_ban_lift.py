"""Tests for 0139_floor_ban_lift against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head, the LIFTED status (8) in the CHECK, `reputation_floor_ban_lift()`
(every in-force status lifted, refused/lifted untouched, tenant isolation, idempotent, fail-closed
inputs, audit columns), that a re-enqueue after a lift is enforced again and forgets the lift,
that claim never re-dispatches a lifted row, erasure of a lifted row needing no suppression code,
and that PUBLIC cannot execute the lift function.
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
_PATH = _ROOT / "alembic" / "versions" / "0139_floor_ban_lift.py"
_LIFT = "SELECT reputation_floor_ban_lift(%s, %s::uuid[], %s::uuid)"
_IN_FORCE = (1, 2, 3, 4, 5, 7)


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0139", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_the_release_head(self) -> None:
        m = _load()
        assert m.revision == "0139_floor_ban_lift"
        assert len(m.revision) <= 32
        assert m.down_revision == "0138_entity_public_ids"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f"diverged alembic heads: {heads}"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0139-floor-ban-lift") as db:
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


@requires_docker
class TestLift:
    @pytest.mark.parametrize("status", _IN_FORCE)
    def test_every_in_force_status_is_lifted_with_audit_columns(
        self, pg_db: PgTestDatabase, status: int
    ) -> None:
        tid, cid = _setup(pg_db, f"t-lift-{status}")
        user, admin = str(uuid.uuid4()), str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, status)
        assert _q(pg_db, _LIFT, (tid, [user], admin))[0][0] == 1
        assert _status(pg_db, rid) == 8
        lifted_at, lifted_by = _q(
            pg_db,
            "SELECT lifted_at, lifted_by::text FROM bundle_reputation_floor_bans WHERE id = %s",
            (rid,),
        )[0]
        assert lifted_at is not None
        assert lifted_by == admin

    def test_refused_and_other_tenants_and_users_are_untouched_and_it_is_idempotent(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-lift-scope")
        other_tid, other_cid = _setup(pg_db, "t-lift-scope-other")
        user, bystander, admin = (str(uuid.uuid4()) for _ in range(3))
        refused = _row(pg_db, tid, cid, user, 6)
        other_tenant = _row(pg_db, other_tid, other_cid, user, 2)
        other_user = _row(pg_db, tid, cid, bystander, 2)
        live = _row(pg_db, tid, cid, user, 2)
        assert _q(pg_db, _LIFT, (tid, [user], admin))[0][0] == 1
        assert _q(pg_db, _LIFT, (tid, [user], admin))[0][0] == 0, (
            "second lift is a no-op"
        )
        assert (_status(pg_db, refused), _status(pg_db, other_tenant)) == (6, 2)
        assert (_status(pg_db, other_user), _status(pg_db, live)) == (2, 8)

    @pytest.mark.parametrize(
        "args",
        [
            (None, ["00000000-0000-4000-8000-000000000001"], str(uuid.uuid4())),
            (1, None, str(uuid.uuid4())),
            (1, [], str(uuid.uuid4())),
            (1, ["00000000-0000-4000-8000-000000000001"], None),
        ],
    )
    def test_invalid_input_fails_closed(
        self, pg_db: PgTestDatabase, args: tuple[Any, ...]
    ) -> None:
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _q(pg_db, _LIFT, args)

    def test_status_check_allows_8_and_rejects_9(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _setup(pg_db, "t-lift-check")
        _row(pg_db, tid, cid, str(uuid.uuid4()), 8)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _row(pg_db, tid, cid, str(uuid.uuid4()), 9)

    def test_public_cannot_execute_the_lift_function(
        self, pg_db: PgTestDatabase
    ) -> None:
        assert (
            _q(
                pg_db,
                "SELECT has_function_privilege('public', "
                "'reputation_floor_ban_lift(bigint, uuid[], uuid)', 'EXECUTE')",
            )[0][0]
            is False
        )


@requires_docker
class TestAfterLift:
    def test_claim_never_redispatches_a_lifted_row(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _setup(pg_db, "t-lift-claim")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 1)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        claimed = _q(pg_db, "SELECT r_id FROM reputation_floor_ban_claim(50, 120)")
        assert rid not in [r[0] for r in claimed]
        assert _status(pg_db, rid) == 8

    def test_erasing_a_lifted_row_needs_no_suppression_code(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-lift-erase")
        user = str(uuid.uuid4())
        rid = _row(pg_db, tid, cid, user, 2)
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        deleted, pseudonymised, pending = _q(
            pg_db,
            "SELECT r_deleted, r_pseudonymised, r_pending FROM "
            "reputation_floor_ban_erase_subject(%s::uuid, %s, 'discord', NULL)",
            (user, tid),
        )[0]
        # 0140 enqueues one platform unban for the delivered ban: a pending row erasure never cancels.
        assert (deleted, pseudonymised) == (0, 1)
        assert pending in (0, 1)
        new_user = _q(
            pg_db,
            "SELECT user_uuid::text FROM bundle_reputation_floor_bans WHERE id = %s",
            (rid,),
        )[0][0]
        assert new_user != user
        assert _status(pg_db, rid) == 8


@requires_docker
class TestRoundTrip:
    def test_downgrade_one_step_then_reupgrade(self, pg_db: PgTestDatabase) -> None:
        """Fresh-DB upgrade to head (the fixture) -> downgrade to 0138 -> upgrade head, lifts intact."""
        tid, cid = _setup(pg_db, "t-lift-roundtrip")
        rid = _row(pg_db, tid, cid, str(uuid.uuid4()), 8)
        alembic_cli("downgrade", "0138_entity_public_ids", dsn=pg_db.dsn)
        cols = _q(
            pg_db,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'bundle_reputation_floor_bans' AND column_name LIKE 'lifted_%%'",
        )
        assert cols == []
        assert _status(pg_db, rid) == 2, "lifted rows return to done on downgrade"
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
        assert _q(
            pg_db,
            "SELECT to_regprocedure('reputation_floor_ban_lift(bigint, uuid[], uuid)')",
        )[0][0]


_ENQUEUE = (
    "SELECT r_state, r_id FROM reputation_floor_ban_enqueue("
    "%s, %s, %s, %s::uuid, %s::smallint, %s, 'discord')"
)


def _denying(db: PgTestDatabase, tid: int, user: str) -> int:
    """Rows that deny access (the `FLOOR_BAN_ACTIVE_STATUSES` set in hub_api)."""
    return _q(
        db,
        "SELECT count(*) FROM bundle_reputation_floor_bans WHERE tenant_id = %s "
        "AND user_uuid = %s::uuid AND status IN (1, 2, 3, 4, 5, 7)",
        (tid, user),
    )[0][0]


def _community_floor(
    db: PgTestDatabase, slug: str, balance: int
) -> tuple[int, int, str]:
    tid, cid = _setup(db, slug)
    user = str(uuid.uuid4())
    _q(
        db,
        "INSERT INTO bundle_reputation_policy (tenant_id, community_id, hard_ban_enabled, "
        "hard_ban_floor) VALUES (%s, %s, true, 400)",
        (tid, cid),
    )
    _q(
        db,
        "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
        "VALUES (%s, %s, %s, %s)",
        (tid, cid, user, balance),
    )
    return tid, cid, user


def _set_balance(db: PgTestDatabase, cid: int, user: str, balance: int) -> None:
    _q(
        db,
        "UPDATE bundle_reputation_scores SET balance = %s WHERE community_id = %s "
        "AND user_uuid = %s::uuid",
        (balance, cid, user),
    )


@requires_docker
class TestLiftIsStickyUntilANewCrossing:
    def test_rerun_with_same_key_and_new_key_while_below_stay_lifted(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = _community_floor(pg_db, "t-sticky-rerun", 300)
        state, rid = _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))[0]
        assert state == 0 and _denying(pg_db, tid, user) == 1
        assert _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))[0][0] == 1
        assert _denying(pg_db, tid, user) == 0
        # The floor job reruns with the SAME key (redelivery): known, still lifted.
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))[0] == (1, rid)
        assert _status(pg_db, rid) == 8
        # ... and with a NEW event key while the user is STILL below the floor: refused.
        state, rid2 = _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0]
        assert state == 3 and rid2 != rid
        assert (
            _q(
                pg_db,
                "SELECT last_error FROM bundle_reputation_floor_bans WHERE id=%s",
                (rid2,),
            )[0][0]
            == "lift_sticky"
        )
        assert _denying(pg_db, tid, user) == 0, "access stays allowed"

    def test_recover_then_fall_below_again_is_a_new_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = _community_floor(pg_db, "t-sticky-recross", 300)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 3
        _set_balance(
            pg_db, cid, user, 450
        )  # recovers above the floor: marks the lift recovered
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_floor_bans WHERE tenant_id = %s "
                "AND status = 8 AND recovered_at IS NOT NULL",
                (tid,),
            )[0][0]
            == 1
        )
        _set_balance(pg_db, cid, user, 350)  # a genuinely NEW floor crossing
        state, rid3 = _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt3"))[0]
        assert state == 0 and _status(pg_db, rid3) == 1
        assert _denying(pg_db, tid, user) == 1, "access denied again"

    def test_lift_while_already_above_the_floor_allows_a_later_new_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid, user = _community_floor(pg_db, "t-sticky-above", 300)
        _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt1"))
        _set_balance(pg_db, cid, user, 500)  # recovered before the admin lifts
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _q(
            pg_db,
            "SELECT recovered_at IS NOT NULL FROM bundle_reputation_floor_bans WHERE tenant_id=%s",
            (tid,),
        )[0][0]
        _set_balance(pg_db, cid, user, 300)
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 1, "evt2"))[0][0] == 0
        assert _denying(pg_db, tid, user) == 1

    def test_tenant_scope_is_sticky_until_the_tenant_score_recovers(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _setup(pg_db, "t-sticky-tenant")
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
            "VALUES (%s, %s, 350)",
            (tid, user),
        )
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 2, "evt1"))[0][0] == 0
        _q(pg_db, _LIFT, (tid, [user], str(uuid.uuid4())))
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 2, "evt2"))[0][0] == 3
        assert _denying(pg_db, tid, user) == 0
        _q(
            pg_db,
            "UPDATE bundle_reputation_tenant_scores SET score = 600 WHERE tenant_id = %s",
            (tid,),
        )
        _q(
            pg_db,
            "UPDATE bundle_reputation_tenant_scores SET score = 380 WHERE tenant_id = %s",
            (tid,),
        )
        assert _q(pg_db, _ENQUEUE, (tid, cid, cid, user, 2, "evt3"))[0][0] == 0
        assert _denying(pg_db, tid, user) == 1
