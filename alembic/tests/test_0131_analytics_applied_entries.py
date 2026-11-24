"""0131_analytics_applied_entries: chain, DDL, grants (writer only), downgrade."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0131_analytics_applied_entries.py"
_REV = "0131_analytics_applied_entries"
_PREV = "0130_analytics_exclusions_grant"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mig0131", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One migrated-to-head Postgres for the whole module."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0131-analytics-applied-entries") as database:
        yield database


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0131_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REV in {
        r.revision for r in script.walk_revisions(base="base", head=heads[0])
    }


def test_upgrade_is_idempotent_ddl_and_grants_only_the_writer_role() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    sql = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "CREATE SCHEMA IF NOT EXISTS analytics_ops" in sql
    assert "CREATE TABLE IF NOT EXISTS analytics_ops.applied_entries" in sql
    targets = [
        ln.strip() for ln in sql.splitlines() if " TO " in ln and "PUBLIC" not in ln
    ]
    assert targets and all(t.rstrip(";").endswith("analytics_writer") for t in targets)
    # tier grant: column-level, role-guarded, exactly the three columns
    assert "pg_roles WHERE rolname = 'analytics_writer'" in sql
    assert "GRANT SELECT (community_id, tier_code, released_at)" in sql
    assert "ON public.community_tier_allocations TO analytics_writer" in sql


def test_downgrade_revokes_exactly_the_tier_columns_and_drops_the_ledger() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.downgrade()
    sql = "\n".join(c.args[0] for c in execute.call_args_list)
    assert "REVOKE SELECT (community_id, tier_code, released_at)" in sql
    assert "DROP TABLE IF EXISTS analytics_ops.applied_entries" in sql


def _member_cursor(db: PgTestDatabase):  # type: ignore[no-untyped-def]
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP ROLE IF EXISTS waddles_svc_analytics_t")
    cur.execute("CREATE ROLE waddles_svc_analytics_t NOLOGIN IN ROLE analytics_writer")
    return conn, cur


def test_member_role_inherits_ledger_access_and_other_roles_cannot(
    db: PgTestDatabase,
) -> None:
    conn, cur = _member_cursor(db)
    try:
        cur.execute("SET ROLE waddles_svc_analytics_t")
        cur.execute(
            "INSERT INTO analytics_ops.applied_entries (entry_ms, entry_seq) VALUES (1, 1)"
        )
        cur.execute(
            "INSERT INTO analytics_ops.applied_entries (entry_ms, entry_seq) VALUES (1, 1) "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute("SELECT count(*) FROM analytics_ops.applied_entries")
        assert cur.fetchone()[0] == 1
        cur.execute("DELETE FROM analytics_ops.applied_entries")
        cur.execute("RESET ROLE")
        for role in ("analytics_reader", "trend_builder", "trend_exporter"):
            cur.execute(f"SET ROLE {role}")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("SELECT 1 FROM analytics_ops.applied_entries")
            cur.execute("RESET ROLE")
    finally:
        conn.close()


def test_tier_grant_is_column_level_and_inherited_by_the_member_role(
    db: PgTestDatabase,
) -> None:
    conn, cur = _member_cursor(db)
    try:
        cur.execute("SET ROLE waddles_svc_analytics_t")
        cur.execute(
            "SELECT community_id, tier_code, released_at "
            "FROM public.community_tier_allocations WHERE released_at IS NULL"
        )
        # every other column of the table, and any other table, stays unreadable
        for sql in (
            "SELECT id FROM public.community_tier_allocations",
            "SELECT tenant_id FROM public.community_tier_allocations",
            "SELECT allocated_by FROM public.community_tier_allocations",
            "SELECT * FROM public.community_tier_allocations",
            "SELECT 1 FROM public.communities",
            "SELECT 1 FROM public.tenant_license_pools",
            "SELECT 1 FROM public.hub_users",
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(sql)
        # and the read-only analytics role did not gain it
        cur.execute("RESET ROLE")
        cur.execute("SET ROLE analytics_reader")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("SELECT community_id FROM public.community_tier_allocations")
    finally:
        conn.close()
