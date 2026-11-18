"""0129_analytics_foundation: chain, DDL, CHECKs, registry, role grants, downgrade guard.

Self-contained: one module-scoped real-Postgres container, tests are order-dependent only within
this file (data-mutating / downgrade tests are LAST), as `--dist loadfile` requires.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest import mock

import psycopg2
import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0129_analytics_foundation.py"
_REV = "0129_analytics_foundation"
_PREV = "0128_dsar_pending_draft"

FP_TABLES = (
    "community_hourly",
    "member_daily",
    "member_sessions",
    "cohort_weekly",
    "member_signals_daily",
)
USER_KEYED = ("member_daily", "member_sessions", "member_signals_daily")
GOV_TABLES = (
    "analytics_dataset_registry",
    "data_use_exclusions",
    "tenant_data_programme_optin",
    "export_recipients",
    "platform_clearance",
    "export_runs",
)
FORBIDDEN_TREND_COLUMNS = frozenset(
    {
        "community_id",
        "user_uuid",
        "user_id",
        "channel_id",
        "platform_user_id",
        "hub_user_id",
    }
)
_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0129", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forbidden_columns(cur, schema: str) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """`(table, column)` for every column in `schema` named like a community/user dimension."""
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND column_name = ANY(%s) ORDER BY 1, 2",
        (schema, sorted(FORBIDDEN_TREND_COLUMNS)),
    )
    return [(str(t), str(c)) for t, c in cur.fetchall()]


def columns_in_schema(cur, schema: str) -> int:  # type: ignore[no-untyped-def]
    """Denominator: how many columns the introspection actually examined."""
    cur.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s",
        (schema,),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One migrated-to-head Postgres for the whole module."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0129-analytics-foundation") as database:
        yield database


@pytest.fixture
def cur(db: PgTestDatabase):  # type: ignore[no-untyped-def]
    """Autocommit superuser cursor (role-restricted tests use `as_role`)."""
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


@contextmanager
def as_role(cursor, role: str):  # type: ignore[no-untyped-def]
    """`SET ROLE role` for the block, always reset."""
    cursor.execute(f"SET ROLE {role}")
    try:
        yield
    finally:
        cursor.execute("RESET ROLE")


def _denied(cursor, role: str, sql: str) -> None:  # type: ignore[no-untyped-def]
    with as_role(cursor, role):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cursor.execute(sql)


def _allowed(cursor, role: str, sql: str) -> None:  # type: ignore[no-untyped-def]
    with as_role(cursor, role):
        cursor.execute(sql)


# --------------------------------------------------------------------------- static / mocked


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0129_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REV in {
        r.revision for r in script.walk_revisions(base="base", head=heads[0])
    }


def test_upgrade_emits_idempotent_schema_qualified_ddl() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    sql = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "lock_timeout" in sql
    assert "CREATE SCHEMA IF NOT EXISTS analytics_fp" in sql
    assert "CREATE SCHEMA IF NOT EXISTS analytics_trend" in sql
    for table in FP_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS analytics_fp.{table}" in sql
    assert "CREATE TABLE IF NOT EXISTS analytics_trend.tenant_trend" in sql
    assert "ck_tenant_trend_k_anonymity CHECK (cohort_size >= 50)" in sql
    assert "REVOKE ALL ON SCHEMA analytics_fp FROM PUBLIC" in sql


@pytest.mark.parametrize(
    ("held", "env", "refuses"),
    [
        ({"export_runs": 1}, None, True),
        ({"export_runs": 1}, "0", True),
        ({"export_runs": 1}, "1", False),
        ({}, None, False),
    ],
)
def test_downgrade_guard_unit(
    monkeypatch: pytest.MonkeyPatch,
    held: dict[str, int],
    env: str | None,
    refuses: bool,
) -> None:
    module = _load()
    monkeypatch.delenv("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", raising=False)
    if env is not None:
        monkeypatch.setenv("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", env)
    with (
        mock.patch.object(module, "_held", return_value=held),
        mock.patch.object(module.op, "execute") as execute,
    ):
        if refuses:
            with pytest.raises(RuntimeError, match="Refusing to drop analytics tables"):
                module.downgrade()
            execute.assert_not_called()
        else:
            module.downgrade()
            assert execute.call_count >= 12


# --------------------------------------------------------------------------- real Postgres


@requires_docker
def test_schemas_tables_exist_and_governance_columns(cur) -> None:  # type: ignore[no-untyped-def]
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics_fp'"
    )
    assert {r[0] for r in cur.fetchall()} == set(FP_TABLES)
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics_trend'"
    )
    assert {r[0] for r in cur.fetchall()} == {"tenant_trend"}
    for table in FP_TABLES:
        cur.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'analytics_fp' AND table_name = %s",
            (table,),
        )
        cols = dict(cur.fetchall())
        for gov in (
            "purpose_code",
            "classification_code",
            "retention_days",
            "provenance_code",
        ):
            assert cols.get(gov) == "NO", (table, gov)
        assert "created_at" in cols
    for table in GOV_TABLES:
        cur.execute("SELECT to_regclass(%s)", (table,))
        assert cur.fetchone()[0] is not None


@requires_docker
def test_nothing_is_enabled_by_default(cur) -> None:  # type: ignore[no-untyped-def]
    for table in (
        "data_use_exclusions",
        "tenant_data_programme_optin",
        "export_recipients",
        "platform_clearance",
        "export_runs",
        "analytics_trend.tenant_trend",
        *(f"analytics_fp.{t}" for t in FP_TABLES),
    ):
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0, table


@requires_docker
def test_registry_seeded_for_every_analytics_table(cur) -> None:  # type: ignore[no-untyped-def]
    cur.execute(
        "SELECT dataset, layer, purpose_code, classification_code, retention_days, min_k, "
        "lineage, notes FROM analytics_dataset_registry"
    )
    rows = {r[0]: r for r in cur.fetchall()}
    expected = {f"analytics_fp.{t}" for t in FP_TABLES} | {
        "analytics_trend.tenant_trend"
    }
    assert set(rows) == expected and len(rows) == 6
    for ds in expected:
        cur.execute("SELECT to_regclass(%s)", (ds,))
        assert cur.fetchone()[0] is not None, f"registry names missing table {ds}"
    trend = rows["analytics_trend.tenant_trend"]
    assert trend[1:6] == (2, 2, 1, 400, 50)
    assert trend[4] <= max(
        rows[f"analytics_fp.{t}"][4] for t in FP_TABLES
    )  # <= input retention
    assert (
        trend[6]["export_enabled"] is False and "no_age_signal" in trend[6]["excludes"]
    )
    signals = rows["analytics_fp.member_signals_daily"]
    assert signals[6]["profiling"] is True and signals[6]["admin_only"] is True
    assert signals[6]["automated_legal_effect_decisions"] is False
    assert "waddles:analytics" in rows["analytics_fp.member_daily"][6]["source"]
    for ds in (f"analytics_fp.{t}" for t in USER_KEYED):
        assert rows[ds][3] == 2  # pseudonymous
    for ds in ("analytics_fp.community_hourly", "analytics_fp.cohort_weekly"):
        assert rows[ds][3] == 1  # aggregate_anonymous


@requires_docker
def test_check_constraints(cur) -> None:  # type: ignore[no-untyped-def]
    cur.execute("INSERT INTO tenants (slug) VALUES ('t-an') RETURNING id")
    tid = cur.fetchone()[0]
    ins = (
        "INSERT INTO analytics_trend.tenant_trend (tenant_id, week, metric_code, dim_code, "
        "dim_value_code, value, cohort_size, provenance_code) VALUES (%s, '2026-11-09', 1, 0, 0, "
        "5, %s, %s)"
    )
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(ins, (tid, 49, 1))  # k-1: below threshold cannot even be stored
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(ins, (tid, 50, 6))  # provenance out of range
    cur.execute(ins, (tid, 50, 1))  # exactly k is allowed
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "UPDATE analytics_trend.tenant_trend SET cohort_size = 10 WHERE tenant_id = %s",
            (tid,),
        )
    hourly = (
        "INSERT INTO analytics_fp.community_hourly (tenant_id, community_id, hour_ts, "
        "metric_code, platform_code, value{extra}) VALUES (1, 1, now(), 1, {plat}, 1{vals})"
    )
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(hourly.format(extra="", plat=6, vals=""))
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(hourly.format(extra=", retention_days", plat=1, vals=", 401"))
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(hourly.format(extra=", purpose_code", plat=1, vals=", 2"))
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(hourly.format(extra=", provenance_code", plat=1, vals=", 0"))
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO data_use_exclusions (user_uuid, reason_code) VALUES (%s, 6)",
            (str(_USER),),
        )
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO tenant_data_programme_optin (tenant_id, terms_version, status_code) "
            "VALUES (%s, 1, 3)",
            (tid,),
        )
    with pytest.raises(psycopg2.errors.CheckViolation):  # layer 2 must carry k >= 50
        cur.execute(
            "INSERT INTO analytics_dataset_registry (dataset, layer, purpose_code, "
            "classification_code, retention_days, min_k) VALUES ('x', 2, 2, 1, 10, 49)"
        )
    with pytest.raises(psycopg2.errors.CheckViolation):  # export_runs enums
        cur.execute(
            "INSERT INTO export_runs (dataset, mode_code, decision_code) "
            "VALUES ('analytics_trend.tenant_trend', 3, 2)"
        )


@requires_docker
def test_trend_schema_has_no_community_or_user_columns(cur) -> None:  # type: ignore[no-untyped-def]
    assert (
        columns_in_schema(cur, "analytics_trend") >= 9
    )  # denominator: really examined
    assert forbidden_columns(cur, "analytics_trend") == []
    # and layer A DOES carry them, proving the helper can see such columns at all
    assert ("member_daily", "user_uuid") in forbidden_columns(cur, "analytics_fp")


@requires_docker
def test_forbidden_column_helper_can_fail(cur) -> None:  # type: ignore[no-untyped-def]
    """Mutation check: the introspection gate must flag a synthetic offending table."""
    cur.execute("CREATE SCHEMA mut_trend")
    try:
        cur.execute(
            "CREATE TABLE mut_trend.ok (tenant_id int, week date, value bigint)"
        )
        assert forbidden_columns(cur, "mut_trend") == []
        cur.execute(
            "CREATE TABLE mut_trend.bad (tenant_id int, community_id int, user_uuid uuid)"
        )
        assert forbidden_columns(cur, "mut_trend") == [
            ("bad", "community_id"),
            ("bad", "user_uuid"),
        ]
    finally:
        cur.execute("DROP SCHEMA mut_trend CASCADE")


@requires_docker
def test_roles_are_nologin_and_public_is_revoked(cur) -> None:  # type: ignore[no-untyped-def]
    cur.execute(
        "SELECT rolname, rolcanlogin, rolsuper FROM pg_roles WHERE rolname = ANY(%s)",
        (["analytics_writer", "analytics_reader", "trend_builder", "trend_exporter"],),
    )
    rows = cur.fetchall()
    assert len(rows) == 4
    assert all(not login and not sup for _, login, sup in rows)
    for schema in ("analytics_fp", "analytics_trend"):
        cur.execute(
            "SELECT nspacl::text FROM pg_namespace WHERE nspname = %s", (schema,)
        )
        acl = cur.fetchone()[0] or "{}"
        assert not any(entry.startswith("=") for entry in acl.strip("{}").split(","))


@requires_docker
def test_trend_exporter_cannot_touch_per_user_data(cur) -> None:  # type: ignore[no-untyped-def]
    cur.execute(
        "INSERT INTO analytics_fp.member_daily (community_id, user_uuid, day) "
        "VALUES (1, %s, '2026-11-17')",
        (str(_USER),),
    )
    cur.execute(
        "INSERT INTO data_use_exclusions (user_uuid, reason_code) VALUES (%s, 1)",
        (str(_USER),),
    )
    for table in FP_TABLES:
        _denied(cur, "trend_exporter", f"SELECT * FROM analytics_fp.{table}")
    for table in (
        "data_use_exclusions",
        "tenant_data_programme_optin",
        "export_runs",
        "analytics_dataset_registry",
    ):
        _denied(cur, "trend_exporter", f"SELECT * FROM {table}")
    _denied(
        cur,
        "trend_exporter",
        "INSERT INTO analytics_fp.member_daily (community_id, user_uuid, day) "
        "VALUES (2, gen_random_uuid(), '2026-11-17')",
    )
    _denied(
        cur,
        "trend_exporter",
        "INSERT INTO analytics_trend.tenant_trend (tenant_id, week, "
        "metric_code, dim_code, dim_value_code, value, cohort_size, provenance_code) "
        "VALUES (1, '2026-11-09', 1, 0, 0, 1, 50, 1)",
    )
    _allowed(cur, "trend_exporter", "SELECT * FROM analytics_trend.tenant_trend")
    _allowed(
        cur,
        "trend_exporter",
        "INSERT INTO export_runs (dataset, mode_code, decision_code, deny_codes) VALUES "
        "('analytics_trend.tenant_trend', 1, 2, ARRAY['EXPORT_NOT_IMPLEMENTED'])",
    )


@requires_docker
def test_analytics_reader_is_read_only(cur) -> None:  # type: ignore[no-untyped-def]
    for table in FP_TABLES:
        _allowed(cur, "analytics_reader", f"SELECT * FROM analytics_fp.{table}")
    _denied(cur, "analytics_reader", "SELECT * FROM analytics_trend.tenant_trend")
    _denied(cur, "analytics_reader", "SELECT * FROM data_use_exclusions")
    _denied(
        cur,
        "analytics_reader",
        "INSERT INTO analytics_fp.cohort_weekly (community_id, "
        "cohort_week, week_offset, active_members, cohort_size) VALUES (1, '2026-11-09', 0, 1, 1)",
    )
    _denied(cur, "analytics_reader", "UPDATE analytics_fp.member_daily SET msgs = 1")
    _denied(cur, "analytics_reader", "DELETE FROM analytics_fp.member_daily")


@requires_docker
def test_analytics_writer_grants(cur) -> None:  # type: ignore[no-untyped-def]
    _allowed(
        cur,
        "analytics_writer",
        "INSERT INTO analytics_fp.cohort_weekly (community_id, cohort_week, week_offset, "
        "active_members, cohort_size) VALUES (9, '2026-11-09', 0, 1, 1)",
    )
    _allowed(
        cur, "analytics_writer", "UPDATE analytics_fp.cohort_weekly SET cohort_size = 2"
    )
    _allowed(cur, "analytics_writer", "DELETE FROM analytics_fp.cohort_weekly")
    _allowed(cur, "analytics_writer", "SELECT * FROM data_use_exclusions")
    _denied(
        cur,
        "analytics_writer",
        "INSERT INTO data_use_exclusions (user_uuid, reason_code) "
        "VALUES (gen_random_uuid(), 1)",
    )
    _denied(cur, "analytics_writer", "SELECT * FROM analytics_trend.tenant_trend")


@requires_docker
def test_trend_builder_cannot_write_first_party(cur) -> None:  # type: ignore[no-untyped-def]
    _allowed(cur, "trend_builder", "SELECT * FROM analytics_fp.member_daily")
    _allowed(cur, "trend_builder", "SELECT * FROM data_use_exclusions")
    _allowed(cur, "trend_builder", "SELECT * FROM tenant_data_programme_optin")
    for table in (
        "member_sessions",
        "member_signals_daily",
        "community_hourly",
        "cohort_weekly",
    ):
        _denied(cur, "trend_builder", f"SELECT * FROM analytics_fp.{table}")
    for write in (
        "INSERT INTO analytics_fp.member_daily (community_id, user_uuid, day) "
        "VALUES (3, gen_random_uuid(), '2026-11-17')",
        "UPDATE analytics_fp.member_daily SET msgs = 5",
        "DELETE FROM analytics_fp.member_daily",
        "INSERT INTO data_use_exclusions (user_uuid, reason_code) VALUES (gen_random_uuid(), 1)",
    ):
        _denied(cur, "trend_builder", write)
    _allowed(
        cur, "trend_builder", "DELETE FROM analytics_trend.tenant_trend WHERE false"
    )
    _denied(cur, "trend_builder", "SELECT * FROM export_runs")


@requires_docker
def test_upgrade_is_idempotent_rerun_on_a_populated_database(
    db: PgTestDatabase,
) -> None:
    """Re-executing every upgrade statement is a no-op: no error, no duplicate, no data loss."""
    module = _load()
    engine = sa.create_engine(db.dsn)
    try:

        def run(statement: str) -> None:
            with engine.begin() as conn:
                conn.execute(sa.text(statement))

        before = _counts(db)
        with mock.patch.object(module.op, "execute", side_effect=run):
            module.upgrade()
            module.upgrade()
        assert _counts(db) == before
        assert (
            before["analytics_dataset_registry"] == 6
            and before["analytics_fp.member_daily"] >= 1
        )
    finally:
        engine.dispose()


def _counts(db: PgTestDatabase) -> dict[str, int]:
    conn = psycopg2.connect(db.dsn)
    try:
        cur = conn.cursor()
        out = {}
        for table in (
            "analytics_dataset_registry",
            "analytics_fp.member_daily",
            "analytics_trend.tenant_trend",
            "data_use_exclusions",
        ):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = int(cur.fetchone()[0])
        return out
    finally:
        conn.close()


@requires_docker
def test_zz_downgrade_refuses_when_non_empty_then_roundtrips(
    db: PgTestDatabase,
    cur,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.delenv("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", raising=False)
    with pytest.raises(Exception):  # noqa: B017 - CLI wrapper error type varies
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
    cur.execute("SELECT count(*) FROM analytics_fp.member_daily")
    assert cur.fetchone()[0] >= 1  # fail closed, data intact
    cur.execute("SELECT to_regclass('analytics_fp.member_daily')")
    assert cur.fetchone()[0] is not None
    alembic_cli(
        "downgrade",
        _PREV,
        dsn=db.dsn,
        env_overrides={"ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE": "1"},
    )
    for table in (
        "analytics_fp.member_daily",
        "analytics_trend.tenant_trend",
        "export_runs",
    ):
        cur.execute("SELECT to_regclass(%s)", (table,))
        assert cur.fetchone()[0] is None
    cur.execute(
        "SELECT count(*) FROM pg_namespace WHERE nspname IN ('analytics_fp','analytics_trend')"
    )
    assert cur.fetchone()[0] == 0
    alembic_cli(
        "upgrade", "head", dsn=db.dsn
    )  # up again (roles already exist: idempotent)
    cur.execute("SELECT count(*) FROM analytics_dataset_registry")
    assert cur.fetchone()[0] == 6
    # empty tables: downgrade needs no override
    alembic_cli("downgrade", _PREV, dsn=db.dsn)
    alembic_cli("upgrade", "head", dsn=db.dsn)
