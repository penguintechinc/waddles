"""Tests for 0097_reputation_unify against a real Postgres (`pg_docker.py` harness).

Contract under test: REPUTATION_CONTRACT.md (PR #1236 SQL/hub-api, PR #1238 Rust). Covers chain
position / single head and `reputation_apply()` (membership, input validation, per-kind and
per-app caps, idempotency, clamp, tenant fixed bounds, auto-clear), the soft-ban functions
(floor wrappers vs hub-api-only manual set/clear, quota), role privilege boundaries and
`reputation_active_logic_app()`.
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
_PATH = _ROOT / "alembic" / "versions" / "0097_reputation_unify.py"
_APPLY = (
    "SELECT new_score, applied FROM reputation_apply("
    "%s, %s, %s::uuid, %s, %s::smallint, %s::smallint, %s, %s, %s, %s)"
)
_BUNDLE_LOGIN = "rep_test_bundle"
_HUB_LOGIN = "rep_test_hub"
_LOGIN_PW = "rep-test-only-pw"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0097", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains(self) -> None:
        m = _load()
        assert m.revision == "0097_reputation_unify"
        assert len(m.revision) <= 32
        assert m.down_revision in {
            "0095_bundle_disabled_apps_view",
            "0096_system_tenant_global_admin",
        }

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        # 0097 is an ancestor of the single head (later migrations chain after it).
        assert heads == ["0099_reputation_floor_ban"]
        script = ScriptDirectory.from_config(cfg)
        assert "0097_reputation_unify" in {
            r.revision for r in script.walk_revisions("base", heads[0])
        }


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0097-reputation") as db:
        with _connect(db) as conn, conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
            )
            cur.execute(
                "INSERT INTO app_catalog (app_id) VALUES ('app-a'), ('app-b') "
                "ON CONFLICT DO NOTHING"
            )
            for login, group in (
                (_BUNDLE_LOGIN, "waddles_bundle_reputation"),
                (_HUB_LOGIN, "hub_api"),
            ):
                cur.execute(
                    f"CREATE ROLE {login} LOGIN PASSWORD %s IN ROLE {group}",
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
            "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
            (slug, tid),
        )
        return tid, cur.fetchone()[0]


def _member(db: PgTestDatabase, cid: int, *, active: bool = True) -> str:
    """Add an active member with a fresh `user_uuid` token and return it."""
    user = str(uuid.uuid4())
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO community_members (community_id, user_id, user_uuid, is_active) "
            "VALUES (%s, %s, %s, %s)",
            (cid, user[:8], user, active),
        )
    return user


def _policy(db: PgTestDatabase, tid: int, cid: int, **cols: Any) -> None:
    names = ", ".join(["tenant_id", "community_id", *cols])
    marks = ", ".join(["%s"] * (2 + len(cols)))
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO bundle_reputation_policy ({names}) VALUES ({marks})",
            (tid, cid, *cols.values()),
        )


def _apply(  # type: ignore[no-untyped-def]
    db,
    tid,
    cid,
    user,
    delta,
    *,
    key=None,
    actor="system:bundle",
    cls=1,
    code=1,
    app=None,
    source="evt",
    login=None,
):
    key = key if key is not None else f"k.{uuid.uuid4().hex}"
    with _connect(db, login) as conn, conn.cursor() as cur:
        cur.execute(_APPLY, (tid, cid, user, delta, cls, code, app, actor, source, key))
        return cur.fetchone()


def _q(db: PgTestDatabase, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _ledger_count(db: PgTestDatabase, cid: int, kind: str | None = None) -> int:
    if kind is None:
        return _q(
            db,
            "SELECT count(*) FROM bundle_reputation_adjustments WHERE community_id=%s",
            (cid,),
        )[0][0]
    return _q(
        db,
        "SELECT count(*) FROM bundle_reputation_adjustments WHERE community_id=%s AND kind=%s",
        (cid, kind),
    )[0][0]


@requires_docker
class TestReputationApply:
    def test_unscored_member_starts_at_600_and_adds_delta(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-start")
        assert _apply(pg_db, tid, cid, _member(pg_db, cid), 25) == (625, True)

    def test_clamps_to_300_850_and_ledger_keeps_requested_vs_applied(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-clamp")
        user = _member(pg_db, cid)
        assert _apply(
            pg_db, tid, cid, user, 100000, cls=3, actor=str(uuid.uuid4())
        ) == (850, True)
        assert _apply(
            pg_db, tid, cid, user, -100000, cls=3, actor=str(uuid.uuid4())
        ) == (300, True)
        rows = _q(
            pg_db,
            "SELECT delta, requested_value, balance_after, kind FROM "
            "bundle_reputation_adjustments WHERE community_id=%s ORDER BY id",
            (cid,),
        )
        assert rows == [
            (250, 100000, 850, "admin_override"),
            (-550, -100000, 300, "admin_override"),
        ]

    def test_non_member_is_rejected_not_a_member_42501(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-nonmember")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="not_a_member"):
            _apply(pg_db, tid, cid, str(uuid.uuid4()), 5)
        left = _member(pg_db, cid, active=False)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="not_a_member"):
            _apply(pg_db, tid, cid, left, 5)
        assert _ledger_count(pg_db, cid) == 0
        assert (
            _q(
                pg_db,
                "SELECT count(*) FROM bundle_reputation_scores WHERE community_id=%s",
                (cid,),
            )[0][0]
            == 0
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"source": "Bad Event"},
            {"source": ""},
            {"source": "x" * 129},
            {"source": "UPPER"},
            {"key": "has space"},
            {"key": "x" * 129},
            {"key": "semi;colon"},
            {"actor": "admin:7"},
            {"actor": "system:Has-Dash"},
            {"actor": "bundle:x"},
            {"cls": 9},
            {"cls": 0},
        ],
    )
    def test_invalid_input_is_22023(
        self, pg_db: PgTestDatabase, kwargs: dict[str, Any]
    ) -> None:
        tid, cid = _scope(pg_db, f"t-inval-{abs(hash(str(kwargs))) % 10**8}")
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _apply(pg_db, tid, cid, _member(pg_db, cid), 5, **kwargs)

    def test_zero_delta_only_allowed_for_admin_override(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-zero")
        user = _member(pg_db, cid)
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _apply(pg_db, tid, cid, user, 0)
        assert _apply(pg_db, tid, cid, user, 0, cls=3, actor=str(uuid.uuid4())) == (
            600,
            True,
        )

    def test_admin_actor_is_stored_as_uuid_and_kind_admin_override(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-admin")
        admin = str(uuid.uuid4())
        _apply(pg_db, tid, cid, _member(pg_db, cid), -30, cls=3, code=101, actor=admin)
        row = _q(
            pg_db,
            "SELECT kind, actor, source_event, reason_code_id, app_id FROM "
            "bundle_reputation_adjustments WHERE community_id=%s",
            (cid,),
        )[0]
        assert row == ("admin_override", admin, "evt", 101, None)

    def test_app_id_is_stored_on_the_ledger(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-appid")
        _apply(pg_db, tid, cid, _member(pg_db, cid), 5, app="app-a")
        assert _q(
            pg_db,
            "SELECT app_id, kind FROM bundle_reputation_adjustments WHERE community_id=%s",
            (cid,),
        ) == [("app-a", "adjust")]

    def test_per_user_activity_cap_is_enforced(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-usercap")
        _policy(pg_db, tid, cid, activity_user_daily_cap=30)
        user = _member(pg_db, cid)
        assert _apply(pg_db, tid, cid, user, 25) == (625, True)
        with pytest.raises(
            psycopg2.errors.ProgramLimitExceeded, match="daily_cap_exceeded"
        ):
            _apply(pg_db, tid, cid, user, 10)
        assert _apply(pg_db, tid, cid, user, 5) == (
            630,
            True,
        )  # exactly at the cap is fine

    def test_per_app_cap_is_enforced_per_app_id(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-appcap")
        _policy(pg_db, tid, cid, activity_app_daily_cap=30)
        u1, u2 = _member(pg_db, cid), _member(pg_db, cid)
        assert _apply(pg_db, tid, cid, u1, 20, app="app-a") == (620, True)
        with pytest.raises(
            psycopg2.errors.ProgramLimitExceeded, match="daily_cap_exceeded"
        ):
            _apply(pg_db, tid, cid, u2, 20, app="app-a")  # app-a total would be 40 > 30
        assert _apply(pg_db, tid, cid, u2, 20, app="app-b") == (
            620,
            True,
        )  # separate app budget

    def test_positive_moderation_rejected_unless_admin_override(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-modpos")
        user = _member(pg_db, cid)
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _apply(pg_db, tid, cid, user, 10, cls=2)
        assert _apply(pg_db, tid, cid, user, -10, cls=2) == (590, True)
        assert _apply(pg_db, tid, cid, user, 10, cls=3, actor=str(uuid.uuid4())) == (
            600,
            True,
        )
        assert _ledger_count(pg_db, cid, "moderation") == 1

    def test_moderation_cap_is_separate_from_activity_cap(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-modcap")
        _policy(
            pg_db, tid, cid, activity_user_daily_cap=10, moderation_user_daily_cap=30
        )
        user = _member(pg_db, cid)
        assert _apply(pg_db, tid, cid, user, -20, cls=2) == (580, True)
        with pytest.raises(psycopg2.errors.ProgramLimitExceeded):
            _apply(
                pg_db, tid, cid, user, -20, cls=2
            )  # moderation budget (30) exhausted
        assert _apply(pg_db, tid, cid, user, 10) == (
            590,
            True,
        )  # activity budget untouched
        with pytest.raises(psycopg2.errors.ProgramLimitExceeded):
            _apply(pg_db, tid, cid, user, 5)  # activity budget (10) exhausted
        assert _apply(pg_db, tid, cid, user, -10, cls=2) == (
            580,
            True,
        )  # moderation 30 used

    def test_admin_override_is_uncapped(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-uncapped")
        _policy(pg_db, tid, cid, activity_user_daily_cap=1)
        user = _member(pg_db, cid)
        assert _apply(pg_db, tid, cid, user, 100, cls=3, actor=str(uuid.uuid4())) == (
            700,
            True,
        )

    def test_replay_returns_applied_false_and_writes_no_ledger_row(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-idem")
        user = _member(pg_db, cid)
        assert _apply(pg_db, tid, cid, user, 10, key="k1") == (610, True)
        assert _ledger_count(pg_db, cid) == 1
        assert _apply(pg_db, tid, cid, user, 10, key="k1") == (610, False)
        assert _ledger_count(pg_db, cid) == 1
        assert _apply(pg_db, tid, cid, user, 10, key="k2") == (620, True)

    def test_concurrent_applies_serialize_and_never_lose_updates(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-conc")
        user = _member(pg_db, cid)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                _apply(pg_db, tid, cid, user, 1, key=f"c{i % 10}", app="app-a")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert _q(
            pg_db,
            "SELECT balance, adjustment_count FROM bundle_reputation_scores "
            "WHERE community_id=%s AND user_uuid=%s",
            (cid, user),
        ) == [(610, 10)]

    def test_tenant_isolation_rejects_foreign_community(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid_a, _ = _scope(pg_db, "t-iso-a")
        _, cid_b = _scope(pg_db, "t-iso-b")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _apply(pg_db, tid_a, cid_b, _member(pg_db, cid_b), 5)

    def test_tenant_aggregate_follows_the_applies(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-agg")
        user = _member(pg_db, cid)
        _apply(pg_db, tid, cid, user, 20)
        _apply(pg_db, tid, cid, user, 5)
        assert _q(
            pg_db,
            "SELECT score, event_count FROM bundle_reputation_tenant_scores "
            "WHERE tenant_id=%s AND user_uuid=%s",
            (tid, user),
        ) == [(625, 2)]

    def test_tenant_aggregate_uses_fixed_300_850_bounds_not_community_scale(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-aggbounds")
        _policy(pg_db, tid, cid, score_min=0, score_max=2000, score_default=600)
        user = _member(pg_db, cid)
        admin = str(uuid.uuid4())
        assert _apply(pg_db, tid, cid, user, 1000, cls=3, actor=admin) == (1600, True)
        agg = "SELECT score FROM bundle_reputation_tenant_scores WHERE tenant_id=%s AND user_uuid=%s"
        assert _q(pg_db, agg, (tid, user)) == [(850,)]
        assert _apply(pg_db, tid, cid, user, -1550, cls=3, actor=admin) == (50, True)
        assert _q(pg_db, agg, (tid, user)) == [(300,)]

    def test_policy_row_changes_the_scale(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-policy")
        _policy(pg_db, tid, cid, score_max=700)
        assert _apply(
            pg_db, tid, cid, _member(pg_db, cid), 500, cls=3, actor=str(uuid.uuid4())
        ) == (
            700,
            True,
        )

    def test_policy_rejects_inverted_thresholds(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-badpolicy")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _policy(pg_db, tid, cid, hard_ban_floor=500, soft_ban_floor=400)

    def test_ledger_kind_check_allows_exactly_the_contract_kinds(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-kinds")
        for kind in (
            "adjust",
            "moderation",
            "admin_override",
            "soft_ban",
            "soft_ban_clear",
        ):
            _q(
                pg_db,
                "INSERT INTO bundle_reputation_adjustments (tenant_id, community_id, "
                "target_user_uuid, scope, delta, reason_code, kind) "
                "VALUES (%s, %s, 'u', 'community', 0, '1', %s)",
                (tid, cid, kind),
            )
        with pytest.raises(psycopg2.errors.CheckViolation):
            _q(
                pg_db,
                "INSERT INTO bundle_reputation_adjustments (tenant_id, community_id, "
                "target_user_uuid, scope, delta, reason_code, kind) "
                "VALUES (%s, %s, 'u', 'community', 0, '1', 'override')",
                (tid, cid),
            )


@requires_docker
class TestRoleBoundaries:
    def test_public_cannot_execute_anything(self, pg_db: PgTestDatabase) -> None:
        for sig in (
            "reputation_apply(bigint,bigint,uuid,int,smallint,smallint,text,text,text,text)",
            "reputation_set_soft_ban(bigint,bigint,uuid,smallint,text,timestamptz)",
            "reputation_clear_soft_ban(bigint,bigint,uuid,text,bool)",
            "reputation_set_floor_soft_ban(bigint,bigint,uuid)",
            "reputation_clear_floor_soft_ban(bigint,bigint,uuid)",
            "reputation_is_soft_banned(bigint,bigint,uuid)",
            "reputation_active_logic_app(bigint)",
        ):
            assert _q(
                pg_db, "SELECT has_function_privilege('public', %s, 'EXECUTE')", (sig,)
            ) == [(False,)], sig

    def test_bundle_role_has_no_manual_set_or_clear_execute(
        self, pg_db: PgTestDatabase
    ) -> None:
        for sig, want in (
            (
                "reputation_set_soft_ban(bigint,bigint,uuid,smallint,text,timestamptz)",
                False,
            ),
            ("reputation_clear_soft_ban(bigint,bigint,uuid,text,bool)", False),
            ("reputation_set_floor_soft_ban(bigint,bigint,uuid)", True),
            ("reputation_clear_floor_soft_ban(bigint,bigint,uuid)", True),
            (
                "reputation_apply(bigint,bigint,uuid,int,smallint,smallint,text,text,text,text)",
                True,
            ),
        ):
            assert _q(
                pg_db,
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (_BUNDLE_LOGIN, sig),
            ) == [(want,)], sig

    def test_hub_api_role_can_execute_manual_set_and_clear(
        self, pg_db: PgTestDatabase
    ) -> None:
        for sig in (
            "reputation_set_soft_ban(bigint,bigint,uuid,smallint,text,timestamptz)",
            "reputation_clear_soft_ban(bigint,bigint,uuid,text,bool)",
        ):
            assert _q(
                pg_db,
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (_HUB_LOGIN, sig),
            ) == [(True,)]

    def test_bundle_role_cannot_call_manual_set_or_clear(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-bundle-manual")
        user = _member(pg_db, cid)
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:  # noqa: SIM117
            with pytest.raises(
                psycopg2.errors.InsufficientPrivilege, match="permission denied"
            ):
                cur.execute(
                    "SELECT reputation_set_soft_ban(%s, %s, %s::uuid, 2::smallint, %s, NULL)",
                    (tid, cid, user, str(uuid.uuid4())),
                )
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:  # noqa: SIM117
            with pytest.raises(
                psycopg2.errors.InsufficientPrivilege, match="permission denied"
            ):
                cur.execute(
                    "SELECT reputation_clear_soft_ban(%s, %s, %s::uuid, 'system:floor', false)",
                    (tid, cid, user),
                )

    def test_bundle_role_cannot_use_admin_override_or_human_actor(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-bundle-priv")
        user = _member(pg_db, cid)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _apply(pg_db, tid, cid, user, 5, cls=3, login=_BUNDLE_LOGIN)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _apply(
                pg_db, tid, cid, user, 5, actor=str(uuid.uuid4()), login=_BUNDLE_LOGIN
            )
        assert _apply(pg_db, tid, cid, user, 5, login=_BUNDLE_LOGIN) == (605, True)

    @pytest.mark.parametrize(
        ("table", "dml"),
        [
            (
                "bundle_reputation_scores",
                "INSERT INTO bundle_reputation_scores "  # noqa: ISC004
                "(tenant_id, community_id, user_uuid, balance) VALUES (1, 1, gen_random_uuid(), 9)",
            ),
            (
                "bundle_reputation_scores",
                "UPDATE bundle_reputation_scores SET balance = 850",
            ),
            ("bundle_reputation_scores", "DELETE FROM bundle_reputation_scores"),
            (
                "bundle_reputation_adjustments",
                "INSERT INTO bundle_reputation_adjustments "  # noqa: ISC004
                "(tenant_id, community_id, target_user_uuid, scope, delta, reason_code) "
                "VALUES (1, 1, 'u', 'community', 1, '1')",
            ),
            (
                "bundle_reputation_adjustments",
                "UPDATE bundle_reputation_adjustments SET delta = 0",
            ),
            (
                "bundle_reputation_tenant_policy",
                "INSERT INTO bundle_reputation_tenant_policy (tenant_id) VALUES (1)",
            ),
            (
                "bundle_reputation_tenant_policy",
                "UPDATE bundle_reputation_tenant_policy SET tenant_auto_ban_floor = 301",
            ),
            (
                "bundle_reputation_soft_bans",
                "INSERT INTO bundle_reputation_soft_bans "  # noqa: ISC004
                "(tenant_id, community_id, user_uuid, reason_code, set_by) "
                "VALUES (1, 1, gen_random_uuid(), 1, 'system:floor')",
            ),
            ("bundle_reputation_soft_bans", "DELETE FROM bundle_reputation_soft_bans"),
            (
                "bundle_reputation_policy",
                "INSERT INTO bundle_reputation_policy "  # noqa: ISC004
                "(tenant_id, community_id) VALUES (1, 1)",
            ),
            (
                "bundle_reputation_policy",
                "UPDATE bundle_reputation_policy SET score_max = 850",
            ),
            (
                "bundle_reputation_tenant_scores",
                "UPDATE bundle_reputation_tenant_scores SET score = 850",
            ),
            (
                "bundle_reputation_tenant_scores",
                "INSERT INTO bundle_reputation_tenant_scores "  # noqa: ISC004
                "(tenant_id, user_uuid) VALUES (1, gen_random_uuid())",
            ),
        ],
    )
    def test_bundle_role_has_no_direct_write_on_any_reputation_table(
        self, pg_db: PgTestDatabase, table: str, dml: str
    ) -> None:
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:  # noqa: SIM117
            with pytest.raises(
                psycopg2.errors.InsufficientPrivilege, match="permission denied"
            ):
                cur.execute(dml)
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert _q(
                pg_db,
                "SELECT has_table_privilege(%s, %s, %s)",
                (_BUNDLE_LOGIN, table, priv),
            ) == [(False,)]

    def test_bundle_role_keeps_select_and_apply_still_works(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-bundle-ro")
        user = _member(pg_db, cid)
        assert _apply(pg_db, tid, cid, user, 7, login=_BUNDLE_LOGIN) == (607, True)
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM bundle_reputation_scores WHERE community_id=%s",
                (cid,),
            )
            assert cur.fetchall() == [(607,)]
            cur.execute("SELECT count(*) FROM bundle_reputation_adjustments")
            cur.execute("SELECT count(*) FROM bundle_reputation_policy")
            cur.execute("SELECT count(*) FROM bundle_reputation_soft_bans")

    def test_hub_api_role_may_use_admin_override(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-hub-priv")
        assert _apply(
            pg_db,
            tid,
            cid,
            _member(pg_db, cid),
            5,
            cls=3,
            actor=str(uuid.uuid4()),
            login=_HUB_LOGIN,
        ) == (605, True)


def _soft_ban(db, tid, cid, user, *, reason=2, set_by=None, expires=None, login=None):  # type: ignore[no-untyped-def]
    set_by = set_by or (str(uuid.uuid4()) if reason == 2 else "system:floor")
    with _connect(db, login) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reputation_set_soft_ban(%s, %s, %s::uuid, %s::smallint, %s, %s)",
            (tid, cid, user, reason, set_by, expires),
        )


def _clear(db, tid, cid, user, *, set_by=None, only_floor=False):  # type: ignore[no-untyped-def]
    set_by = set_by or ("system:floor" if only_floor else str(uuid.uuid4()))
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reputation_clear_soft_ban(%s, %s, %s::uuid, %s, %s)",
            (tid, cid, user, set_by, only_floor),
        )
        return cur.fetchone()[0]


def _is_soft_banned(db, tid, cid, user) -> bool:  # type: ignore[no-untyped-def]
    return bool(
        _q(db, "SELECT reputation_is_soft_banned(%s, %s, %s)", (tid, cid, user))[0][0]
    )


@requires_docker
class TestSoftBans:
    def test_set_then_read_then_clear_deletes_the_row_and_ledgers_history(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sb1")
        user = _member(pg_db, cid)
        admin = str(uuid.uuid4())
        assert _is_soft_banned(pg_db, tid, cid, user) is False
        _soft_ban(pg_db, tid, cid, user, set_by=admin)
        assert _is_soft_banned(pg_db, tid, cid, user) is True
        assert _clear(pg_db, tid, cid, user, set_by=admin) is True
        assert _clear(pg_db, tid, cid, user, set_by=admin) is False
        assert _is_soft_banned(pg_db, tid, cid, user) is False
        # current state only: clear == DELETE; history lives in the ledger
        assert _q(
            pg_db,
            "SELECT count(*) FROM bundle_reputation_soft_bans WHERE community_id=%s",
            (cid,),
        ) == [(0,)]
        assert _q(
            pg_db,
            "SELECT kind, actor, reason_code_id FROM bundle_reputation_adjustments "
            "WHERE community_id=%s ORDER BY id",
            (cid,),
        ) == [("soft_ban", admin, 2), ("soft_ban_clear", admin, 2)]

    def test_soft_ban_pk_is_community_user(self, pg_db: PgTestDatabase) -> None:
        pk = _q(
            pg_db,
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'bundle_reputation_soft_bans'::regclass "
            "AND i.indisprimary ORDER BY a.attname",
        )
        assert pk == [("community_id",), ("user_uuid",)]

    def test_manual_set_validates_actor_membership_and_expiry(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sb2")
        user = _member(pg_db, cid)
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _soft_ban(
                pg_db, tid, cid, user, set_by="admin:1"
            )  # numeric/free-text actor
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _soft_ban(
                pg_db, tid, cid, user, set_by="system:floor"
            )  # system actor on manual
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            _soft_ban(pg_db, tid, cid, user, expires="2000-01-01T00:00:00Z")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="not_a_member"):
            _soft_ban(pg_db, tid, cid, str(uuid.uuid4()))

    def test_expired_soft_ban_is_not_banned(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-sb3")
        user = _member(pg_db, cid)
        _soft_ban(pg_db, tid, cid, user, expires="2999-01-01T00:00:00Z")
        assert _is_soft_banned(pg_db, tid, cid, user) is True
        _q(
            pg_db,
            "UPDATE bundle_reputation_soft_bans SET expires_at = '2000-01-01' WHERE community_id=%s",
            (cid,),
        )
        assert _is_soft_banned(pg_db, tid, cid, user) is False

    def test_is_scoped_to_its_community(self, pg_db: PgTestDatabase) -> None:
        tid, cid = _scope(pg_db, "t-sb4")
        _, other_cid = _scope(pg_db, "t-sb4b")
        user = _member(pg_db, cid)
        _soft_ban(pg_db, tid, cid, user)
        assert _is_soft_banned(pg_db, tid, other_cid, user) is False

    def test_foreign_community_is_rejected(self, pg_db: PgTestDatabase) -> None:
        tid_a, _ = _scope(pg_db, "t-sb5a")
        _, cid_b = _scope(pg_db, "t-sb5b")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _soft_ban(pg_db, tid_a, cid_b, _member(pg_db, cid_b))

    def test_manual_quota_blocks_manual_sets_and_clears_but_never_floor(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sbquota")
        _policy(
            pg_db,
            tid,
            cid,
            manual_soft_ban_daily_quota=1,
            soft_ban_enabled=True,
            soft_ban_floor=550,
        )
        manual_user, floor_user = _member(pg_db, cid), _member(pg_db, cid)
        _soft_ban(pg_db, tid, cid, manual_user)  # consumes the whole manual quota (1)
        with pytest.raises(
            psycopg2.errors.ProgramLimitExceeded, match="quota_exceeded"
        ):
            _soft_ban(pg_db, tid, cid, _member(pg_db, cid))
        with pytest.raises(
            psycopg2.errors.ProgramLimitExceeded, match="quota_exceeded"
        ):
            _clear(
                pg_db, tid, cid, manual_user
            )  # manual clear counts too, and rolls back
        assert _is_soft_banned(pg_db, tid, cid, manual_user) is True
        # floor path (bundle role, via the wrapper) is NEVER blocked by the manual quota
        _apply(
            pg_db, tid, cid, floor_user, -60, cls=3, actor=str(uuid.uuid4())
        )  # 540 < 550
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reputation_set_floor_soft_ban(%s, %s, %s::uuid)",
                (tid, cid, floor_user),
            )
        assert _is_soft_banned(pg_db, tid, cid, floor_user) is True

    def _floor_clear(self, db, tid, cid, user):  # type: ignore[no-untyped-def]
        with _connect(db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reputation_clear_floor_soft_ban(%s, %s, %s::uuid)",
                (tid, cid, user),
            )
            return cur.fetchone()[0]

    def _floor_ban(self, db, tid, cid, user):  # type: ignore[no-untyped-def]
        """Put `user` under a floor ban (score 540 < 550), as the bundle role would."""
        _apply(db, tid, cid, user, -60, cls=3, actor=str(uuid.uuid4()))
        with _connect(db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reputation_set_floor_soft_ban(%s, %s, %s::uuid)",
                (tid, cid, user),
            )

    def test_floor_clear_refused_while_still_breached(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-fl-breached")
        _policy(pg_db, tid, cid, soft_ban_enabled=True, soft_ban_floor=550)
        user = _member(pg_db, cid)
        self._floor_ban(pg_db, tid, cid, user)
        with pytest.raises(
            psycopg2.errors.InvalidParameterValue, match="floor_still_breached"
        ):
            self._floor_clear(pg_db, tid, cid, user)
        assert _is_soft_banned(pg_db, tid, cid, user) is True

    def test_floor_clear_allowed_after_recovery_or_when_admin_disabled_auto_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-fl-recover")
        _policy(pg_db, tid, cid, soft_ban_enabled=True, soft_ban_floor=550)
        recovered, disabled = _member(pg_db, cid), _member(pg_db, cid)
        for u in (recovered, disabled):
            self._floor_ban(pg_db, tid, cid, u)
        # recovery without going through apply (which would auto-clear on its own)
        _q(
            pg_db,
            "UPDATE bundle_reputation_scores SET balance = 600 WHERE community_id=%s AND user_uuid=%s",
            (cid, recovered),
        )
        assert self._floor_clear(pg_db, tid, cid, recovered) is True
        assert _is_soft_banned(pg_db, tid, cid, recovered) is False
        with pytest.raises(
            psycopg2.errors.InvalidParameterValue, match="floor_still_breached"
        ):
            self._floor_clear(pg_db, tid, cid, disabled)
        _q(
            pg_db,
            "UPDATE bundle_reputation_policy SET soft_ban_enabled = false WHERE community_id=%s",
            (cid,),
        )
        assert self._floor_clear(pg_db, tid, cid, disabled) is True
        assert (
            self._floor_clear(pg_db, tid, cid, disabled) is False
        )  # nothing left to lift

    def test_floor_clear_never_touches_a_manual_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-fl-manual")
        user = _member(pg_db, cid)
        _soft_ban(pg_db, tid, cid, user)  # manual
        assert self._floor_clear(pg_db, tid, cid, user) is False
        assert _is_soft_banned(pg_db, tid, cid, user) is True

    def test_floor_lift_quota_per_user_and_per_community(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-fl-quota")
        _policy(
            pg_db,
            tid,
            cid,
            soft_ban_enabled=False,
            floor_lift_user_daily_quota=1,
            floor_lift_community_daily_quota=2,
        )
        u1, u2, u3 = (_member(pg_db, cid) for _ in range(3))
        for u in (u1, u2, u3):
            _soft_ban(pg_db, tid, cid, u, reason=1)
        assert self._floor_clear(pg_db, tid, cid, u1) is True
        _soft_ban(pg_db, tid, cid, u1, reason=1)
        with pytest.raises(
            psycopg2.errors.ProgramLimitExceeded, match="quota_exceeded"
        ):
            self._floor_clear(pg_db, tid, cid, u1)  # per-user (1/day)
        assert _is_soft_banned(pg_db, tid, cid, u1) is True
        assert self._floor_clear(pg_db, tid, cid, u2) is True
        with pytest.raises(
            psycopg2.errors.ProgramLimitExceeded, match="quota_exceeded"
        ):
            self._floor_clear(pg_db, tid, cid, u3)  # per-community (2/day)

    def test_floor_wrapper_refuses_unless_floor_actually_breached(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sbwrap")
        user = _member(pg_db, cid)
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:  # noqa: SIM117
            with pytest.raises(
                psycopg2.errors.InvalidParameterValue, match="floor_not_breached"
            ):
                cur.execute(
                    "SELECT reputation_set_floor_soft_ban(%s, %s, %s::uuid)",
                    (tid, cid, user),
                )
        _policy(pg_db, tid, cid, soft_ban_enabled=True, soft_ban_floor=550)
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:  # noqa: SIM117
            with pytest.raises(
                psycopg2.errors.InvalidParameterValue, match="floor_not_breached"
            ):
                cur.execute(
                    "SELECT reputation_set_floor_soft_ban(%s, %s, %s::uuid)",
                    (tid, cid, user),
                )
        _apply(pg_db, tid, cid, user, -60, cls=3, actor=str(uuid.uuid4()))  # 540
        with _connect(pg_db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reputation_set_floor_soft_ban(%s, %s, %s::uuid)",
                (tid, cid, user),
            )
        assert _q(
            pg_db,
            "SELECT reason_code, set_by FROM bundle_reputation_soft_bans WHERE community_id=%s",
            (cid,),
        ) == [(1, "system:floor")]

    def test_floor_clear_wrapper_cannot_remove_a_manual_ban(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sbmanualkeep")
        user = _member(pg_db, cid)
        _soft_ban(pg_db, tid, cid, user)
        assert _clear(pg_db, tid, cid, user, only_floor=True) is False
        assert _is_soft_banned(pg_db, tid, cid, user) is True

    def test_floor_ban_auto_clears_on_recovery_but_manual_never_does(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-sbauto")
        floor_user, manual_user = _member(pg_db, cid), _member(pg_db, cid)
        admin = str(uuid.uuid4())
        for u in (floor_user, manual_user):
            _apply(
                pg_db, tid, cid, u, -100, cls=3, actor=admin
            )  # 500, below the 550 floor
        _soft_ban(pg_db, tid, cid, floor_user, reason=1)
        _soft_ban(pg_db, tid, cid, manual_user, reason=2)
        _apply(pg_db, tid, cid, floor_user, 10)  # 510: still below -> stays banned
        assert _is_soft_banned(pg_db, tid, cid, floor_user) is True
        _apply(pg_db, tid, cid, floor_user, 60)  # 570 >= 550 -> floor ban clears
        _apply(pg_db, tid, cid, manual_user, 70)  # 570 -> manual ban stays
        assert _is_soft_banned(pg_db, tid, cid, floor_user) is False
        assert _is_soft_banned(pg_db, tid, cid, manual_user) is True
        assert _q(
            pg_db,
            "SELECT actor, source_event FROM bundle_reputation_adjustments "
            "WHERE community_id=%s AND kind='soft_ban_clear'",
            (cid,),
        ) == [("system:floor", "soft_ban.auto_clear")]

    def test_ledger_rows_keep_their_community_for_attribution(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid = _scope(pg_db, "t-attr")
        _apply(pg_db, tid, cid, _member(pg_db, cid), 5)
        assert _q(
            pg_db,
            "SELECT tenant_id, community_id FROM bundle_reputation_adjustments WHERE community_id=%s",
            (cid,),
        ) == [(tid, cid)]

    def test_two_communities_each_move_the_tenant_aggregate(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, cid_a = _scope(pg_db, "t-two")
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES ('b', %s) RETURNING id",
                (tid,),
            )
            cid_b = cur.fetchone()[0]
        user = str(uuid.uuid4())
        for cid in (cid_a, cid_b):
            with _connect(pg_db) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO community_members (community_id, user_id, user_uuid, is_active) "
                    "VALUES (%s, %s, %s, true)",
                    (cid, user[:8], user),
                )
        _apply(pg_db, tid, cid_a, user, -40, cls=2)
        _apply(pg_db, tid, cid_b, user, -30, cls=2)
        assert _q(
            pg_db,
            "SELECT score FROM bundle_reputation_tenant_scores WHERE tenant_id=%s AND user_uuid=%s",
            (tid, user),
        ) == [(530,)]


@requires_docker
class TestActiveLogicApp:
    def _install(
        self,
        db: PgTestDatabase,
        tid: int,
        app: str,
        manifest: str,
        *,
        avail: bool = True,
    ) -> None:
        with _connect(db) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (app,),
            )
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                "VALUES (%s, '1.0.0', 'python', 'prebuilt') RETURNING id",
                (app,),
            )
            vid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO app_global_installs (app_id, version, version_id, permission_hash, "
                "summary_json) VALUES (%s, '1.0.0', %s, %s, '{}'::jsonb)",
                (app, vid, "sha256:" + "0" * 64),
            )
            cur.execute(
                "INSERT INTO app_version_uploads (app_id, version, tenant_id, artifact_kind, "
                "language, manifest_json) VALUES (%s, '1.0.0', %s, 'prebuilt', 'python', %s::jsonb)",
                (app, tid, manifest),
            )
            cur.execute(
                "INSERT INTO bundle_tenant_availability (tenant_id, app_id, available) "
                "VALUES (%s, %s, %s)",
                (tid, app, avail),
            )

    def _active(self, db: PgTestDatabase, tid: int) -> Any:
        with _connect(db, _BUNDLE_LOGIN) as conn, conn.cursor() as cur:
            cur.execute("SELECT reputation_active_logic_app(%s)", (tid,))
            return cur.fetchone()[0]

    def test_none_then_one_then_ambiguous_fails_closed(
        self, pg_db: PgTestDatabase
    ) -> None:
        tid, _ = _scope(pg_db, "t-logic")
        assert self._active(pg_db, tid) is None
        self._install(pg_db, tid, "logic-a", '{"provides": "reputation-logic"}')
        self._install(pg_db, tid, "not-logic", '{"provides": ["other"]}')
        assert self._active(pg_db, tid) == "logic-a"
        self._install(pg_db, tid, "logic-b", '{"provides": ["x", "reputation-logic"]}')
        assert self._active(pg_db, tid) is None  # ambiguous -> NULL (fail closed)

    def test_unavailable_logic_app_is_not_active(self, pg_db: PgTestDatabase) -> None:
        tid, _ = _scope(pg_db, "t-logic-off")
        self._install(
            pg_db, tid, "logic-off", '{"provides": "reputation-logic"}', avail=False
        )
        assert self._active(pg_db, tid) is None


@requires_docker
class TestBackfill:
    def test_backfill_is_idempotent_and_skips_unresolvable(
        self, pg_db: PgTestDatabase
    ) -> None:
        _, cid = _scope(pg_db, "t-backfill")
        user = str(uuid.uuid4())
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE community_members ADD COLUMN IF NOT EXISTS reputation NUMERIC"
            )
            cur.execute(
                "INSERT INTO community_members (community_id, user_id, user_uuid, reputation) "
                "VALUES (%s, '42', %s, 777), (%s, '43', NULL, 700)",
                (cid, user, cid),
            )
            for _ in range(2):  # second run must be a no-op
                cur.execute(_load()._BACKFILL_SQL)
            cur.execute(
                "SELECT user_uuid::text, balance FROM bundle_reputation_scores WHERE community_id=%s",
                (cid,),
            )
            assert cur.fetchall() == [(user, 777)]
