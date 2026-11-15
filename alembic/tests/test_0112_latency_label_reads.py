"""Real-Postgres tests for 0112_latency_label_reads (command-latency label registries).

svc-process reads `app_commands (command)` and svc-action reads `app_versions (app_id, language)`
from the read replica under their own column-scoped roles. Grants can only be proven by a live
session as the role, so on a TRUE fresh `alembic` replay to head the Rust statements
(`core/bundle_config_db/src/labels.rs`, extracted from the source so test and code cannot drift)
run as each role; writes and every non-granted column are refused; downgrade REVOKEs exactly the
grants with no column ACL left behind; the catalog (`service-roles.yaml`) agrees with the migration.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
    PgTestDatabase,
    alembic_cli,
    load_service_roles_module,
    migrated_postgres,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent / "versions" / "0112_latency_label_reads.py"
)
_RUST_SOURCE = REPO_ROOT / "core" / "bundle_config_db" / "src" / "labels.rs"
_PRIOR = "0110_merge_precompile_vault"
_PROC, _ACT = "waddles_svc_process", "waddles_svc_action"
APP = "waddles.core.app.ping"


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0112", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(const: str) -> str:
    """A reader statement verbatim from labels.rs (`\\` line continuations joined)."""
    text = _RUST_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf'pub const {const}: &str = "\\\n(.*?)";\n', text, re.DOTALL)
    assert match is not None, f"{const} not found in labels.rs"
    return match.group(1).replace("\\\n", "")


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


def _col_priv(owner: Any, role: str, table: str, column: str) -> bool:
    """Whether `role` holds SELECT on `table.column` (effective)."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT')", (role, table, column)
        )
        return bool(cur.fetchone()[0])


def _seed(db: PgTestDatabase) -> None:
    """Two tenants' registrations of `ping`, one of `roll`, a wildcard, and two versions."""
    conn = _owner(db)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, slug) VALUES (901, 'lbl-a'), (902, 'lbl-b')")
        cur.execute("INSERT INTO app_catalog (app_id) VALUES (%s)", (APP,))
        cur.execute(
            "INSERT INTO app_versions (id, app_id, version, language, artifact_kind, "
            "artifact_digest) VALUES (9001, %s, '1.0.0', 'python', 'prebuilt', 'sha256:l1'), "
            "(9002, %s, '2.0.0', 'rust', 'prebuilt', 'sha256:l2')",
            (APP, APP),
        )
        cur.execute(
            "INSERT INTO app_commands (tenant_id, community_id, app_id, command, platforms) "
            "VALUES (901, 0, %s, 'ping', '[\"discord\"]'), (902, 0, %s, 'ping', '[\"discord\"]'), "
            "(901, 0, %s, 'roll', '[\"discord\"]'), (901, 0, %s, '*', '[\"discord\"]')",
            (APP, APP, APP, APP),
        )
    conn.close()


@pytest.fixture(scope="module")
def real_chain() -> Any:
    """Fresh full-chain Postgres at head (incl. 0112), seeded."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0112-label-reads") as db:
        _seed(db)
        yield db


def test_metadata_single_head_off_0110_merge() -> None:
    """Revision id <= 32 chars, chained off 0110_merge, and the graph has exactly one head."""
    m = _load()
    assert (m.revision, m.down_revision) == ("0112_latency_label_reads", _PRIOR)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, f"alembic heads: {script.get_heads()}"
    assert m.revision in {r.revision for r in script.walk_revisions("base", "heads")}


def test_catalog_declares_the_migration_columns() -> None:
    """`service-roles.yaml` lists every column 0112 grants (reconcile --strict would strip others)."""
    by_name = {r.name: r for r in load_service_roles_module().load_catalog().roles}
    examined = 0
    for role, tables in _load().GRANTS.items():
        for table, cols in tables.items():
            declared = set(by_name[role].columns[table]["SELECT"])
            assert set(cols) <= declared, f"{role}.{table}: catalog lacks {cols}"
            examined += len(cols)
    assert examined == 3, "denominator: 1 svc-process column + 2 svc-action columns"
    assert set(by_name[_PROC].columns["app_commands"]["SELECT"]) == {"command"}


@requires_docker
def test_process_reads_command_names_only(real_chain: PgTestDatabase) -> None:
    """svc-process: the reader statement returns distinct names (widest first, no `*`); every other
    column and every write is refused."""
    conn = _connect(real_chain, _PROC, real_chain.service_role_passwords[_PROC])
    with conn.cursor() as cur:
        cur.execute("PREPARE cmds(bigint) AS " + _sql("COMMANDS_SQL"))
        cur.execute("EXECUTE cmds(%s)", [257])
        assert [r[0] for r in cur.fetchall()] == ["ping", "roll"]
    for stmt in (
        "SELECT tenant_id FROM app_commands",
        "SELECT community_id FROM app_commands",
        "SELECT app_id FROM app_commands",
        "SELECT description FROM app_commands",
        "INSERT INTO app_commands (tenant_id, app_id, command, platforms) "
        "VALUES (901, 'x', 'evil', '[\"d\"]')",
        "UPDATE app_commands SET command = 'x'",
        "DELETE FROM app_commands",
        "SELECT language FROM app_versions",
    ):
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


@requires_docker
def test_action_reads_bundle_language_only(real_chain: PgTestDatabase) -> None:
    """svc-action: one row per bundle with its NEWEST version's language; digests, commands and
    writes are refused."""
    conn = _connect(real_chain, _ACT, real_chain.service_role_passwords[_ACT])
    with conn.cursor() as cur:
        cur.execute("PREPARE bnd(bigint) AS " + _sql("BUNDLES_SQL"))
        cur.execute("EXECUTE bnd(%s)", [257])
        assert cur.fetchall() == [(APP, "rust")]
    for stmt in (
        "SELECT artifact_digest FROM app_versions",
        "SELECT command FROM app_commands",
        "UPDATE app_versions SET language = 'go'",
        "DELETE FROM app_versions",
    ):
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


@requires_docker
def test_downgrade_revokes_exactly_then_upgrade_regrants(
    real_chain: PgTestDatabase,
) -> None:
    """upgrade -> downgrade -> upgrade: the label columns toggle; 0107's `app_versions.id` stays."""
    owner = _owner(real_chain)
    cols = [
        (_PROC, "app_commands", "command"),
        (_ACT, "app_versions", "app_id"),
        (_ACT, "app_versions", "language"),
    ]
    for role, table, col in cols:
        assert _col_priv(owner, role, table, col), f"{role} lacks {table}.{col}"
    alembic_cli("downgrade", _PRIOR, dsn=real_chain.dsn)
    for role, table, col in cols:
        assert not _col_priv(owner, role, table, col), f"{role} kept {table}.{col}"
    assert _col_priv(owner, _ACT, "app_versions", "id"), "0107's grant must survive"
    alembic_cli("upgrade", "head", dsn=real_chain.dsn)
    for role, table, col in cols:
        assert _col_priv(owner, role, table, col)
    owner.close()
