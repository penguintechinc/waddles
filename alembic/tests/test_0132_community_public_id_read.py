"""Real-Postgres tests for 0132_community_public_id_read.

svc-process reads `communities (id, tenant_id, public_id)` -- and nothing else on that table -- from
the read replica so the host can hand bundles the community's PUBLIC UUID. The Rust statement
(`PUBLIC_ID_SQL`, extracted from the source so test and code cannot drift) runs as the real
service role: the verified (tenant, community) pair resolves to its UUID, a mismatched tenant or an
unknown id resolves to nothing, every other column and every write is refused, downgrade REVOKEs
exactly the grant, and the catalog (`service-roles.yaml`) agrees with the migration.
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
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0132_community_public_id_read.py"
)
_RUST_SOURCE = (
    REPO_ROOT / "core" / "bundle_config_db" / "src" / "community_public_id.rs"
)
_PRIOR = "0131_analytics_applied_entries"
_PROC = "waddles_svc_process"
PUB = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f01"


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0132", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql() -> str:
    """`PUBLIC_ID_SQL` verbatim from the Rust source (`\\` line continuations joined)."""
    text = _RUST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'pub const PUBLIC_ID_SQL: &str = "\\\n(.*?)";\n', text, re.DOTALL)
    assert match is not None, "PUBLIC_ID_SQL not found in community_public_id.rs"
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


def _col_priv(owner: Any, column: str) -> bool:
    """Whether svc-process holds SELECT on `communities.column` (effective)."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege(%s, 'communities', %s, 'SELECT')",
            (_PROC, column),
        )
        return bool(cur.fetchone()[0])


@pytest.fixture(scope="module")
def real_chain() -> Any:
    """Fresh full-chain Postgres at head (incl. 0132), seeded with two tenants' communities."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0132-pubid-read") as db:
        conn = _owner(db)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, slug) VALUES (931, 'pid-a'), (932, 'pid-b')"
            )
            cur.execute(
                "INSERT INTO communities (id, name, tenant_id, public_id) VALUES "
                "(9310, 'c1', 931, %s), (9320, 'other', 932, "
                "'7f3c2d10-aaaa-4bbb-8ccc-0123456789ab')",
                (PUB,),
            )
        conn.close()
        yield db


def test_metadata_single_head_off_0131() -> None:
    """Revision id <= 32 chars, chained off 0131, and the graph has exactly one head."""
    m = _load()
    assert (m.revision, m.down_revision) == ("0132_community_public_id_read", _PRIOR)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, f"alembic heads: {script.get_heads()}"


def test_catalog_declares_exactly_the_migration_columns() -> None:
    """`service-roles.yaml` lists exactly the columns 0132 grants (reconcile --strict)."""
    by_name = {r.name: r for r in load_service_roles_module().load_catalog().roles}
    cols = _load().GRANTS[_PROC]["communities"]
    assert cols == ("id", "tenant_id", "public_id")
    assert tuple(by_name[_PROC].columns["communities"]["SELECT"]) == cols
    assert "communities" not in by_name[_PROC].tables


@requires_docker
def test_process_resolves_the_public_id_and_nothing_else(
    real_chain: PgTestDatabase,
) -> None:
    """The Rust statement as svc-process: right pair -> UUID; wrong tenant/unknown -> no row; every
    other column and every write refused."""
    conn = _connect(real_chain, _PROC, real_chain.service_role_passwords[_PROC])
    with conn.cursor() as cur:
        cur.execute("PREPARE pid(bigint, bigint) AS " + _sql())
        cur.execute("EXECUTE pid(%s, %s)", [9310, 931])
        assert cur.fetchall() == [(PUB,)]
        cur.execute("EXECUTE pid(%s, %s)", [9310, 932])  # mismatched tenant
        assert cur.fetchall() == []
        cur.execute("EXECUTE pid(%s, %s)", [424242, 931])  # unknown community
        assert cur.fetchall() == []
    for stmt in (
        "SELECT name FROM communities",
        "SELECT * FROM communities",
        "UPDATE communities SET public_id = public_id",
        "DELETE FROM communities",
        "INSERT INTO communities (name, tenant_id) VALUES ('x', 931)",
    ):
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


@requires_docker
def test_downgrade_revokes_exactly_then_upgrade_regrants(
    real_chain: PgTestDatabase,
) -> None:
    """upgrade -> downgrade -> upgrade: the three columns toggle, no column ACL is left behind."""
    owner = _owner(real_chain)
    cols = ("id", "tenant_id", "public_id")
    assert all(_col_priv(owner, c) for c in cols)
    alembic_cli("downgrade", _PRIOR, dsn=real_chain.dsn)
    assert not any(_col_priv(owner, c) for c in cols)
    alembic_cli("upgrade", "head", dsn=real_chain.dsn)
    assert all(_col_priv(owner, c) for c in cols)
    owner.close()
