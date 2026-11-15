"""Real-Postgres tests for 0108_revoke_cfg_communities (least privilege, follow-up to 0107).

0107 gave `waddles_svc_process` column `SELECT (id, tenant_id)` on `communities`; the per-service
negative test (`test_0055`, `test_a_role_cannot_reach_another_services_tables`) says svc-process must
NOT read `communities`. The reader statement no longer joins it (tenant isolation comes from the
`tenant_id` filters on the tables it does read), and 0108 removes the grant. Proven here against a
TRUE fresh `alembic` replay (real roles from 0055, every real table) and a bare mini database:

* svc-process cannot read `communities` at head (the exact 0055 negative) and holds no column ACL
  on it; svc-action keeps its pre-existing table-level `communities: SELECT`;
* downgrade 0108 restores exactly the grants (svc-process can read `communities` again), upgrade
  removes them again; downgrade through 0107 leaves no column ACL behind;
* `DROP ROLE` works after downgrading 0108 + 0107 and fails while the grants are present.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import psycopg2.errors
import pytest

# Shared helpers/fixtures of the 0107 suite (same directory on sys.path, like pg_docker).
import test_0107_bundle_config_db_reads as _t0107
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

_column_acl_entries = _t0107._column_acl_entries
_connect = _t0107._connect
_owner = _t0107._owner
#: Re-exported pytest fixture (bare Postgres with just the roles and tables).
mini_db = _t0107.mini_db

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_VERSIONS = Path(__file__).resolve().parent.parent / "versions"
_ROLES = ("waddles_svc_process", "waddles_svc_action")
_PROCESS = "waddles_svc_process"
_ACTION = "waddles_svc_action"
_PRIOR_0107 = "0107_bundle_config_db_reads"
_PRIOR_0106 = "0106_backfill_kv_capability"


def _load(filename: str, name: str) -> ModuleType:
    """Import a migration module by path."""
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _m0108() -> ModuleType:
    return _load("0108_revoke_cfg_communities.py", "migration_0108")


def _m0107() -> ModuleType:
    return _load("0107_bundle_config_db_reads.py", "migration_0107")


def _can_select_communities(db: PgTestDatabase, role: str) -> bool:
    """The exact 0055 negative probe: does `SELECT 1 FROM communities` succeed as `role`?"""
    conn = _connect(db, role, db.service_role_passwords[role])
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT 1 FROM communities")
            except psycopg2.errors.InsufficientPrivilege:
                return False
            return True
    finally:
        conn.close()


def _communities_acl(owner: Any, role: str) -> int:
    """Column-level ACL entries naming `role` on `communities`."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = 'communities' AND c.relnamespace = 'public'::regnamespace "
            "AND a.attacl IS NOT NULL AND a.attacl::text LIKE %s",
            (f"%{role}=%",),
        )
        return int(cur.fetchone()[0])


def test_metadata_head_and_catalog() -> None:
    """id <= 32 chars, chained off 0107, the single head; the catalog has no svc-process communities."""
    m = _m0108()
    assert (m.revision, m.down_revision) == ("0108_revoke_cfg_communities", _PRIOR_0107)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [m.revision]
    by_name = {r.name: r for r in load_service_roles_module().load_catalog().roles}
    assert "communities" not in by_name[_PROCESS].columns
    assert "communities" not in by_name[_PROCESS].tables
    assert "communities" in by_name[_ACTION].tables  # pre-existing, untouched by 0108
    # What 0108 revokes is exactly what 0107 granted svc-process on `communities`.
    assert m.REVOKED == {
        _PROCESS: {"communities": _m0107().GRANTS[_PROCESS]["communities"]}
    }


@pytest.fixture(scope="module")
def real_chain() -> Iterator[PgTestDatabase]:
    """Fresh full-chain Postgres at head (incl. 0107 + 0108)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0108-revoke-communities") as db:
        yield db


@requires_docker
def test_head_denies_svc_process_communities_and_keeps_svc_action(
    real_chain: PgTestDatabase,
) -> None:
    """The 0055 negative holds at head; the reader's other grants are intact."""
    owner = _owner(real_chain)
    assert not _can_select_communities(real_chain, _PROCESS), (
        "svc-process reached communities"
    )
    assert _communities_acl(owner, _PROCESS) == 0
    assert _can_select_communities(real_chain, _ACTION), (
        "svc-action lost its table-level grant"
    )
    with owner.cursor() as cur:
        for role in _ROLES:
            cur.execute(
                "SELECT has_column_privilege(%s, 'bundle_community_config', 'config', 'SELECT')",
                (role,),
            )
            assert cur.fetchone()[0] is True, f"{role} lost the reader's own grant"
    owner.close()


@requires_docker
def test_downgrade_restores_then_upgrade_revokes_and_0107_leaves_nothing(
    real_chain: PgTestDatabase,
) -> None:
    """head -> 0107 (grants back) -> head (revoked) -> 0106 (no column ACL left anywhere)."""
    owner = _owner(real_chain)
    alembic_cli("downgrade", _PRIOR_0107, dsn=real_chain.dsn)
    assert _can_select_communities(real_chain, _PROCESS), (
        "downgrade must restore the grant"
    )
    assert _communities_acl(owner, _PROCESS) == 1 + 1  # id + tenant_id

    alembic_cli("upgrade", "head", dsn=real_chain.dsn)
    assert not _can_select_communities(real_chain, _PROCESS)
    assert _communities_acl(owner, _PROCESS) == 0

    alembic_cli("downgrade", _PRIOR_0106, dsn=real_chain.dsn)
    for role in _ROLES:
        assert _column_acl_entries(owner, role) == 0, f"{role} kept column ACL entries"
    alembic_cli("upgrade", "head", dsn=real_chain.dsn)  # leave the chain at head
    assert not _can_select_communities(real_chain, _PROCESS)
    owner.close()


def _run(db: PgTestDatabase, module: ModuleType, fn_name: str) -> None:
    """Run a migration's upgrade/downgrade with op.execute bound to a live cursor."""
    from unittest.mock import patch

    conn = _owner(db)
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(module, fn_name)()
    conn.close()


@requires_docker
def test_drop_role_works_after_downgrading_0108_and_0107(
    mini_db: PgTestDatabase,
) -> None:
    """DROP ROLE fails while grants remain and succeeds once 0108 + 0107 are downgraded."""
    m107, m108 = _m0107(), _m0108()
    _run(mini_db, m107, "upgrade")
    _run(mini_db, m108, "upgrade")
    _run(mini_db, m108, "upgrade")  # idempotent
    owner = _owner(mini_db)
    assert _communities_acl(owner, _PROCESS) == 0
    for role in _ROLES:
        assert _column_acl_entries(owner, role) > 0
        with (
            owner.cursor() as cur,
            pytest.raises(psycopg2.errors.DependentObjectsStillExist),
        ):
            cur.execute(
                f"DROP ROLE {role}"
            )  # control: leftover column grants block the drop
    _run(mini_db, m108, "downgrade")
    assert _communities_acl(owner, _PROCESS) == 2, (
        "0108 downgrade restores id + tenant_id"
    )
    _run(mini_db, m107, "downgrade")
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
