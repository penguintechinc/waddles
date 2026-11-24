"""0130_analytics_exclusions_grant: chain, role-guarded idempotent GRANT, exact privileges.

The migrated test chain already has the `hub_api` group role (0129 guards on it); the real-Postgres
test runs the migration's own `upgrade()`/`downgrade()` SQL against the real
`public.data_use_exclusions`, and asserts the privileges by acting as the role.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0130_analytics_exclusions_grant.py"
_REV = "0130_analytics_exclusions_grant"
_TOKEN = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rev0130", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV and len(module.revision) <= 32
    assert module.down_revision == "0129_analytics_foundation"


def test_history_has_one_head_containing_0130() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REV in {
        r.revision for r in script.walk_revisions(base="base", head=heads[0])
    }


def test_grant_is_role_guarded_least_privilege_and_has_a_real_downgrade() -> None:
    module = _load()
    assert "pg_roles WHERE rolname = 'hub_api'" in module._GRANT
    assert "SELECT, INSERT, DELETE ON public.data_use_exclusions" in module._GRANT
    assert "UPDATE" not in module._GRANT and "PUBLIC" not in module._GRANT
    assert "REVOKE SELECT, INSERT, DELETE" in module._REVOKE


@pytest.fixture(scope="module")
def cur() -> Iterator[psycopg2.extensions.cursor]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0130-excl-grant") as db:
        conn = psycopg2.connect(db.dsn)
        try:
            conn.autocommit = True
            yield conn.cursor()
        finally:
            conn.close()


def _can(cur: psycopg2.extensions.cursor, sql: str) -> bool:
    cur.execute("SET ROLE hub_api")
    try:
        cur.execute(sql)
        return True
    except psycopg2.errors.InsufficientPrivilege:
        return False
    finally:
        cur.execute("RESET ROLE")


@requires_docker
def test_grant_gives_exactly_select_insert_delete(cur) -> None:  # type: ignore[no-untyped-def]
    module = _load()
    table = "public.data_use_exclusions"
    # `migrated_postgres` upgrades to head, so 0130 has already granted hub_api.
    cur.execute(module._REVOKE)
    assert not _can(cur, f"SELECT 1 FROM {table}")  # PUBLIC was revoked by 0129

    cur.execute(module._GRANT)
    cur.execute(module._GRANT)  # idempotent re-run
    assert _can(cur, f"SELECT 1 FROM {table}")
    assert _can(
        cur, f"INSERT INTO {table} (user_uuid, reason_code) VALUES ('{_TOKEN}', 1)"
    )
    assert not _can(cur, f"UPDATE {table} SET reason_code = 2")
    assert _can(cur, f"DELETE FROM {table} WHERE user_uuid = '{_TOKEN}'")
    assert not _can(cur, "SELECT 1 FROM public.export_runs")  # no other table widened
    assert not _can(cur, "SELECT 1 FROM analytics_fp.community_hourly")

    cur.execute(module._REVOKE)
    assert not _can(cur, f"SELECT 1 FROM {table}")
    cur.execute(module._GRANT)
