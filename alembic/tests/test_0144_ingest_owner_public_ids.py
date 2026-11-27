"""Real-Postgres tests for 0144_ingest_owner_public_ids.

hub-api's EventSub secret route names the subscription's owners by ``public_id`` UUID only, and
svc-ingest resolves them to its numeric keys itself. The Rust statement (``OWNER_IDS_SQL``,
extracted from the source so test and code cannot drift) runs as the real ``waddles_svc_ingest``
role: a community + connection of ONE tenant resolve; a cross-tenant pair or an unknown UUID
resolves to nothing; ``tenants``, every token column and every write stay refused; downgrade
REVOKEs exactly the grant; and the catalog (``service-roles.yaml``) agrees with the migration.
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
    / "0144_ingest_owner_public_ids.py"
)
_RUST_SOURCE = REPO_ROOT / "core" / "svc_ingest" / "src" / "twitch_unban.rs"
_PRIOR = "0143_eventsub_channel_unban"
_ROLE = "waddles_svc_ingest"
C_A = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f41"
C_B = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f42"
N_A = "1c7c1b63-6a1f-4a7b-8b4a-2c7b4e7e0a41"
N_B = "1c7c1b63-6a1f-4a7b-8b4a-2c7b4e7e0a42"
UNKNOWN = "2d8d2c74-7b2a-4b8c-9c5b-3d8c5f8f1b99"


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0144", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql() -> str:
    """`OWNER_IDS_SQL` verbatim from the Rust source (`\\` line continuations joined)."""
    text = _RUST_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r'pub const OWNER_IDS_SQL: &str = "\\\n(.*?)";\n', text, re.DOTALL
    )
    assert match is not None, "OWNER_IDS_SQL not found in twitch_unban.rs"
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


def _col_priv(owner: Any, table: str, column: str) -> bool:
    """Whether svc-ingest holds SELECT on `table.column` (effective)."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT')", (_ROLE, table, column)
        )
        return bool(cur.fetchone()[0])


@pytest.fixture(scope="module")
def real_chain() -> Any:
    """Fresh full-chain Postgres at head (incl. 0144), two tenants each with community + conn."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0144-owner-ids") as db:
        conn = _owner(db)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, slug) VALUES (941, 'oid-a'), (942, 'oid-b')"
            )
            cur.execute(
                "INSERT INTO communities (id, name, tenant_id, public_id) VALUES "
                "(9410, 'ca', 941, %s), (9420, 'cb', 942, %s)",
                (C_A, C_B),
            )
            cur.execute(
                "INSERT INTO platform_connections (id, tenant_id, platform, resource_type, "
                "resource_id, access_token, public_id) VALUES "
                "(94100, 941, 'twitch', 'twitch_channel', 'r-a', 'x', %s), "
                "(94200, 942, 'twitch', 'twitch_channel', 'r-b', 'x', %s)",
                (N_A, N_B),
            )
        conn.close()
        yield db


def test_metadata_single_head_off_0143() -> None:
    """Revision id <= 32 chars, chained off 0143, and the graph has exactly one head."""
    m = _load()
    assert (m.revision, m.down_revision) == ("0144_ingest_owner_public_ids", _PRIOR)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, f"alembic heads: {script.get_heads()}"


def test_catalog_declares_the_migration_columns() -> None:
    """`service-roles.yaml` holds every column 0144 grants, and nothing on `tenants`."""
    by_name = {r.name: r for r in load_service_roles_module().load_catalog().roles}
    grants = _load().GRANTS[_ROLE]
    role = by_name[_ROLE]
    assert set(grants["communities"]) <= set(role.columns["communities"]["SELECT"])
    assert (
        tuple(role.columns["platform_connections"]["SELECT"])
        == grants["platform_connections"]
    )
    assert "tenants" not in role.columns and "tenants" not in role.tables
    assert "platform_connections" not in role.tables


@requires_docker
def test_ingest_resolves_one_tenants_owners_and_nothing_else(
    real_chain: PgTestDatabase,
) -> None:
    """The Rust statement as svc-ingest: same-tenant pair -> numeric keys; cross-tenant or
    unknown -> no row; `tenants`, token columns and every write refused."""
    conn = _connect(real_chain, _ROLE, real_chain.service_role_passwords[_ROLE])
    with conn.cursor() as cur:
        cur.execute("PREPARE oid(text, text) AS " + _sql())
        cur.execute("EXECUTE oid(%s, %s)", [C_A, N_A])
        assert cur.fetchall() == [(9410, 941, 94100)]
        cur.execute("EXECUTE oid(%s, %s)", [C_A, N_B])  # connection of another tenant
        assert cur.fetchall() == []
        cur.execute("EXECUTE oid(%s, %s)", [UNKNOWN, N_A])
        assert cur.fetchall() == []
    for stmt in (
        "SELECT 1 FROM tenants",
        "SELECT access_token FROM platform_connections",
        "SELECT refresh_token FROM platform_connections",
        "SELECT resource_id FROM platform_connections",
        "SELECT * FROM platform_connections",
        "SELECT name FROM communities",
        "UPDATE platform_connections SET public_id = public_id",
        "DELETE FROM platform_connections",
        "UPDATE communities SET public_id = public_id",
    ):
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


@requires_docker
def test_downgrade_revokes_exactly_then_upgrade_regrants(
    real_chain: PgTestDatabase,
) -> None:
    """upgrade -> downgrade -> upgrade: 0144's columns toggle; 0101's grants are untouched."""
    owner = _owner(real_chain)
    added = [("communities", "public_id")] + [
        ("platform_connections", c) for c in ("id", "tenant_id", "public_id")
    ]
    assert all(_col_priv(owner, t, c) for t, c in added)
    alembic_cli("downgrade", _PRIOR, dsn=real_chain.dsn)
    assert not any(_col_priv(owner, t, c) for t, c in added)
    assert _col_priv(owner, "communities", "id") and _col_priv(
        owner, "communities", "tenant_id"
    )
    alembic_cli("upgrade", "head", dsn=real_chain.dsn)
    assert all(_col_priv(owner, t, c) for t, c in added)
    owner.close()
