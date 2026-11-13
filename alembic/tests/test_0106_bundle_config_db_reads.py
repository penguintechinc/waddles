"""Real-Postgres tests for 0106_bundle_config_db_reads (issue #1306).

svc-process / svc-action read per-community bundle config straight from the (replica) database
under their own column-scoped roles instead of calling hub-api. Grants can only be proven by a live
session as the role, so:

* `real_chain` -- a TRUE fresh `alembic` replay to head (roles from 0055, every real table): the
  Rust reader's exact statement (`core/hub_client/src/bundle_config_db.rs::CONFIG_SQL`, extracted
  from the source file) runs as each role against seeded real tables; writes and every
  non-granted column are denied; downgrade REVOKEs exactly the grants (no column ACL left behind);
  upgrade -> downgrade -> upgrade round-trips; the catalog (`service-roles.yaml`) agrees with the
  migration so `service_roles.py reconcile --strict` never strips or adds a grant.
* `mini_db` -- a bare Postgres with only the two roles and the tables: after downgrade `DROP ROLE`
  succeeds (a leftover column grant makes it fail -- a prior bug), and the same DROP ROLE FAILS
  while the grants are still present (the control that proves the check can fail).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
    PgTestDatabase,
    _free_port,
    _wait_ready,
    alembic_cli,
    load_service_roles_module,
    migrated_postgres,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0106_bundle_config_db_reads.py"
)
_RUST_SOURCE = REPO_ROOT / "core" / "hub_client" / "src" / "bundle_config_db.rs"
_PRIOR = "0105_twitch_handle_freshness"
_ROLES = ("waddles_svc_process", "waddles_svc_action")
APP = "waddles.core.app.calendar"

#: The statement's `$n` placeholders are bound through a server-side PREPARE.
_PREPARE = "PREPARE bundle_cfg(bigint[], bigint[], text[]) AS "


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0106", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_sql() -> str:
    """The Rust reader's statement, verbatim from `CONFIG_SQL` (so test and code cannot drift)."""
    text = _RUST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'pub const CONFIG_SQL: &str = "\\\n(.*?)";\n', text, re.DOTALL)
    assert match is not None, "CONFIG_SQL not found in bundle_config_db.rs"
    return match.group(1)


def _connect(db: PgTestDatabase, user: str, password: str) -> Any:
    """Autocommit connection (one denied statement never poisons the next)."""
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=user,
        password=password,
        dbname=db.dbname,
        connect_timeout=10,
    )
    conn.autocommit = True
    return conn


def _owner(db: PgTestDatabase) -> Any:
    """Owner connection (autocommit)."""
    return _connect(db, db.user, db.password)


def _run_config_sql(
    conn: Any, keys: list[tuple[int, int, str]]
) -> list[tuple[Any, ...]]:
    """Execute the reader's statement for `keys` as the connected role."""
    with conn.cursor() as cur:
        cur.execute("DEALLOCATE ALL")
        cur.execute(_PREPARE + _config_sql())
        cur.execute(
            "EXECUTE bundle_cfg(%s::bigint[], %s::bigint[], %s::text[])",
            [[k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys]],
        )
        return list(cur.fetchall())


#: Columns neither role may read (secret/PII/staging) -> table.
_FORBIDDEN_READS = (
    ("updated_by", "bundle_community_config"),
    ("schema_sha256", "bundle_community_config"),
    ("created_at", "bundle_community_config"),
    ("artifact_digest", "app_versions"),
    ("staging_source_key", "app_version_uploads"),
    ("name", "communities"),
)


def _seed_real_tables(db: PgTestDatabase) -> None:
    """Seed one tenant, two communities (+1 in another tenant), an active calendar app with
    schema defaults, a community-specific activation, and two saved config versions."""

    def manifest(default: int) -> str:
        """A manifest whose schema declares one defaulted property."""
        return json.dumps(
            {
                "community_config_schema": {
                    "type": "object",
                    "properties": {
                        "default_duration_minutes": {
                            "type": "integer",
                            "default": default,
                        }
                    },
                }
            }
        )

    conn = _owner(db)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, slug) VALUES (801, 'cfg-a'), (802, 'cfg-b')"
        )
        cur.execute(
            "INSERT INTO communities (id, name, tenant_id) VALUES "
            "(8010, 'c1', 801), (8011, 'c2', 801), (8020, 'other', 802)"
        )
        cur.execute("INSERT INTO app_catalog (app_id) VALUES (%s)", (APP,))
        cur.execute(
            "INSERT INTO app_versions (id, app_id, version, language, artifact_kind, "
            "artifact_digest) VALUES (8001, %s, '1.0.0', 'rust', 'prebuilt', 'sha256:a'), "
            "(8002, %s, '2.0.0', 'rust', 'prebuilt', 'sha256:b')",
            (APP, APP),
        )
        for vid, ver, dur in ((8001, "1.0.0", 60), (8002, "2.0.0", 120)):
            cur.execute(
                "INSERT INTO app_version_uploads (app_id, version, tenant_id, artifact_kind, "
                "language, manifest_json, app_version_id, staging_source_key) "
                "VALUES (%s, %s, 801, 'prebuilt', 'rust', %s::jsonb, %s, 'secret-key')",
                (APP, ver, manifest(dur), vid),
            )
        cur.execute(
            "INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id) "
            "VALUES (%s, 801, 0, 8001), (%s, 801, 8011, 8002)",
            (APP, APP),
        )
        cur.execute(
            "INSERT INTO bundle_community_config (tenant_id, community_id, app_id, version, "
            "config, schema_sha256, updated_by) VALUES "
            "(801, 8010, %s, 1, '{\"default_duration_minutes\": 45}', 'h', NULL), "
            "(801, 8010, %s, 2, '{\"default_duration_minutes\": 30, \"x\": true}', 'h', NULL)",
            (APP, APP),
        )
    conn.close()


@pytest.fixture(scope="module")
def real_chain() -> Iterator[PgTestDatabase]:
    """Fresh full-chain Postgres at head (migrations incl. 0106), seeded."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0106-cfg-reads") as db:
        _seed_real_tables(db)
        yield db


def _col_priv(
    owner: Any, role: str, table: str, column: str, priv: str = "SELECT"
) -> bool:
    """Whether `role` holds `priv` on `table.column` (effective, via has_column_privilege)."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege(%s, %s, %s, %s)", (role, table, column, priv)
        )
        return bool(cur.fetchone()[0])


def _column_acl_entries(owner: Any, role: str) -> int:
    """Column-level ACL entries naming `role` on public tables (what blocks DROP ROLE)."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL "
            "AND a.attacl::text LIKE %s",
            (f"%{role}=%",),
        )
        return int(cur.fetchone()[0])


def test_metadata_single_head_off_0105() -> None:
    """Revision id <= 32 chars, chained off 0105, and the graph has exactly one head."""
    m = _load()
    assert (m.revision, m.down_revision) == ("0106_bundle_config_db_reads", _PRIOR)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == [m.revision], f"alembic heads: {heads}"


def test_catalog_matches_the_migration_grants() -> None:
    """`service-roles.yaml` declares exactly the migration's column grants (reconcile --strict
    re-asserts the catalog, so any difference would strip or add privileges after every upgrade)."""
    catalog = load_service_roles_module().load_catalog()
    by_name = {r.name: r for r in catalog.roles}
    grants = _load().GRANTS
    examined = 0
    for role, tables in grants.items():
        declared = {
            t: tuple(by_name[role].columns[t]["SELECT"]) for t in by_name[role].columns
        }
        assert declared == dict(tables), f"{role}: catalog columns != migration GRANTS"
        examined += len(tables)
    assert examined == 9, "denominator: 5 tables for svc-process + 4 for svc-action"
    # svc-action's tenant gate is the pre-existing table-level communities grant.
    assert "communities" in by_name["waddles_svc_action"].tables


@requires_docker
@pytest.mark.parametrize("role", _ROLES)
def test_role_runs_the_reader_statement_and_nothing_else(
    real_chain: PgTestDatabase, role: str
) -> None:
    """As the real service role: the Rust statement returns hub-api's merged answer; every write
    and every non-granted column is refused."""
    password = real_chain.service_role_passwords[role]
    conn = _connect(real_chain, role, password)
    keys = [
        (801, 8010, APP),  # saved v2 over defaults
        (
            801,
            8011,
            APP,
        ),  # community activation (v2 manifest, default 120), never saved
        (801, 0, APP),  # tenant-wide scope
        (801, 8020, APP),  # community of ANOTHER tenant: absent
        (801, 8010, "waddles.core.app.inactive"),  # not active: absent
    ]
    rows = {
        (r[0], r[1], r[2]): (r[3], json.loads(r[4]))
        for r in _run_config_sql(conn, keys)
    }
    assert rows == {
        (801, 8010, APP): (2, {"default_duration_minutes": 30, "x": True}),
        (801, 8011, APP): (0, {"default_duration_minutes": 120}),
        (801, 0, APP): (0, {"default_duration_minutes": 60}),
    }, "denominator: 3 of 5 scopes resolve"
    forbidden = [
        (
            "INSERT INTO bundle_community_config (tenant_id, community_id, app_id, version, "
            "config, schema_sha256) VALUES (801, 8010, 'x', 9, '{}', 'h')"
        ),
        "UPDATE bundle_community_config SET config = '{}'::jsonb",
        "DELETE FROM bundle_community_config",
        "UPDATE app_active_versions SET version_id = 8001",
        "UPDATE app_version_uploads SET manifest_json = '{}'::jsonb",
    ] + [
        f"SELECT {c} FROM {t}"
        for c, t in _FORBIDDEN_READS
        # svc-action holds table-level `communities: SELECT` (catalog), so `name` is readable
        # there by design; svc-process only has the column-scoped (id, tenant_id).
        if not (role == "waddles_svc_action" and t == "communities")
    ]
    for stmt in forbidden:
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


@requires_docker
def test_downgrade_revokes_exactly_then_upgrade_regrants(
    real_chain: PgTestDatabase,
) -> None:
    """upgrade -> downgrade -> upgrade on the real chain: no column ACL left behind."""
    owner = _owner(real_chain)
    grants = _load().GRANTS
    checked = 0
    for role, tables in grants.items():
        assert _column_acl_entries(owner, role) > 0
        for table, cols in tables.items():
            for col in cols:
                assert _col_priv(owner, role, table, col), f"{role} lacks {table}.{col}"
                checked += 1
    assert checked == 28, "denominator: 15 svc-process + 13 svc-action columns"

    alembic_cli("downgrade", _PRIOR, dsn=real_chain.dsn)
    for role, tables in grants.items():
        for table, cols in tables.items():
            for col in cols:
                if table == "communities":
                    continue  # a designed group role may also grant it; the ACL check below is exact
                assert not _col_priv(owner, role, table, col), (
                    f"{role} kept {table}.{col}"
                )
        assert _column_acl_entries(owner, role) == 0, f"{role} kept column ACL entries"
    # With the grants gone the role can no longer run the reader statement.
    conn = _connect(
        real_chain,
        "waddles_svc_process",
        real_chain.service_role_passwords["waddles_svc_process"],
    )
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        _run_config_sql(conn, [(801, 8010, APP)])
    conn.close()

    alembic_cli("upgrade", "head", dsn=real_chain.dsn)
    for role in _ROLES:
        assert _column_acl_entries(owner, role) > 0
    conn = _connect(
        real_chain,
        "waddles_svc_action",
        real_chain.service_role_passwords["waddles_svc_action"],
    )
    assert len(_run_config_sql(conn, [(801, 8010, APP)])) == 1
    conn.close()
    owner.close()


_MINI_SQL = """
CREATE ROLE waddles_svc_process LOGIN PASSWORD 'testpass123';
CREATE ROLE waddles_svc_action LOGIN PASSWORD 'testpass123';
CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER, name TEXT);
CREATE TABLE app_versions (id BIGINT PRIMARY KEY, artifact_digest TEXT);
CREATE TABLE app_version_uploads (id BIGINT PRIMARY KEY, app_version_id BIGINT,
    manifest_json JSONB, staging_source_key TEXT);
CREATE TABLE app_active_versions (app_id TEXT, tenant_id INTEGER, community_id INTEGER,
    version_id BIGINT);
CREATE TABLE bundle_community_config (id BIGSERIAL PRIMARY KEY, tenant_id INTEGER,
    community_id INTEGER, app_id TEXT, version INTEGER, config JSONB, schema_sha256 TEXT,
    updated_by INTEGER);
"""


@pytest.fixture
def mini_db() -> Iterator[PgTestDatabase]:
    """Bare Postgres 17 with just the roles and tables (fsync off: fast on a loaded disk)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = f"waddles-migtest-0106-mini-{_free_port()}"
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1",
        port=port,
        user="waddlebot",
        password="testpass123",
        dbname="waddlebot",
    )
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={db.user}",
            "-e",
            f"POSTGRES_PASSWORD={db.password}",
            "-e",
            f"POSTGRES_DB={db.dbname}",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:17-bookworm",
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_MINI_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        yield db
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


def _run(db: PgTestDatabase, fn_name: str) -> None:
    """Run the migration's upgrade/downgrade with op.execute bound to a live cursor."""
    conn = _owner(db)
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(_load(), fn_name)()
    conn.close()


@requires_docker
def test_drop_role_succeeds_after_downgrade_and_fails_without_it(
    mini_db: PgTestDatabase,
) -> None:
    """DROP ROLE must work once the migration is downgraded -- and fail while grants remain."""
    _run(mini_db, "upgrade")
    _run(mini_db, "upgrade")  # idempotent
    owner = _owner(mini_db)
    for role in _ROLES:
        assert _column_acl_entries(owner, role) > 0
        with (
            owner.cursor() as cur,
            pytest.raises(psycopg2.errors.DependentObjectsStillExist),
        ):
            cur.execute(
                f"DROP ROLE {role}"
            )  # control: leftover column grants block the drop
    _run(mini_db, "downgrade")
    _run(mini_db, "downgrade")  # idempotent
    for role in _ROLES:
        assert _column_acl_entries(owner, role) == 0
        with owner.cursor() as cur:
            cur.execute(f"DROP ROLE {role}")
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(_ROLES),)
        )
        assert cur.fetchone()[0] == 0
    owner.close()
