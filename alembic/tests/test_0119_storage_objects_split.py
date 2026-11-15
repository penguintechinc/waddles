"""Tests for 0119_storage_objects_split: chain, and the real-Postgres key-rewrite data migration.

Static checks pin the revision chain/id length; the docker test seeds rows keyed by the OLD
`storage.objects` id (policies, grants, approval ceiling, tenant restrictions) and proves an
old DENY lands as a deny on BOTH new keys, nothing keeps the old key, and the downgrade is the
safe (never-widening) inverse.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg2
import psycopg2.extensions
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0119_storage_objects_split.py"
_PREV = "0109_platform_vault"  # a parent of the 0119 merge point (`-1` is ambiguous)
_OLD = "storage.objects"
_READ = "storage.objects.read"
_WRITE = "storage.objects.write"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0119", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert len(module.revision) <= 32
    assert module.down_revision


def test_history_has_exactly_one_head_and_0119_is_in_it() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0119_storage_objects_split" in {r.revision for r in script.walk_revisions()}


def _skip_fk_enforcement(cur: psycopg2.extensions.cursor) -> None:
    """FK parents are irrelevant to a key rewrite: disable FK triggers on the rewritten tables."""
    for table in (
        "instance_permission_policies",
        "community_permission_grants",
        "app_permission_requests",
        "app_tenant_permission_restrictions",
    ):
        cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER ALL")


def _rows(cur: psycopg2.extensions.cursor, sql: str) -> list[tuple[object, ...]]:
    cur.execute(sql)
    return sorted(cur.fetchall(), key=repr)


@requires_docker
def test_old_key_rows_are_rewritten_to_both_new_keys_and_downgrade_never_widens() -> (
    None
):
    with migrated_postgres("0119-storage-split") as db:
        # step back below 0119 so the OLD key can be seeded, then re-apply the migration
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _skip_fk_enforcement(cur)
                cur.execute(
                    "INSERT INTO instance_permission_policies (permission_key, param_scope, action) "
                    "VALUES (%s, NULL, 'deny'), (%s, 'scoped', 'allow')",
                    (_OLD, _OLD),
                )
                # a pre-existing allow on one new key must be escalated to deny, not kept
                cur.execute(
                    "INSERT INTO instance_permission_policies (permission_key, param_scope, action) "
                    "VALUES (%s, NULL, 'allow')",
                    (_READ,),
                )
                cur.execute(
                    "INSERT INTO community_permission_grants "
                    "(community_id, tenant_id, app_id, permission_id) VALUES (1, 1, 'a.b.c', %s)",
                    (_OLD,),
                )
                cur.execute(
                    "INSERT INTO app_permission_requests "
                    "(app_id, version, permission_id, risk, justification) "
                    "VALUES ('a.b.c', '1.0.0', %s, 'normal', 'j')",
                    (_OLD,),
                )
                cur.execute(
                    "INSERT INTO app_tenant_permission_restrictions (tenant_id, app_id, permission_id) "
                    "VALUES (1, 'a.b.c', %s)",
                    (_OLD,),
                )
            alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                pol = _rows(
                    cur,
                    "SELECT permission_key, param_scope, action FROM instance_permission_policies "
                    "WHERE permission_key LIKE 'storage.objects%'",
                )
                assert pol == sorted(
                    [
                        (_READ, None, "deny"),
                        (_WRITE, None, "deny"),
                        (_READ, "scoped", "allow"),
                        (_WRITE, "scoped", "allow"),
                    ],
                    key=repr,
                ), pol
                for table, col in (
                    ("community_permission_grants", "permission_id"),
                    ("app_permission_requests", "permission_id"),
                    ("app_tenant_permission_restrictions", "permission_id"),
                ):
                    keys = [
                        r[0]
                        for r in _rows(
                            cur,
                            f"SELECT {col} FROM {table} WHERE {col} LIKE 'storage.objects%'",
                        )
                    ]
                    assert keys == [_READ, _WRITE], (table, keys)
                cur.execute(
                    "SELECT count(*) FROM instance_permission_policy_audit "
                    "WHERE permission_key IN (%s, %s)",
                    (_READ, _WRITE),
                )
                assert cur.fetchone()[0] >= 2  # type: ignore[index]

            # downgrade: policy collapses to deny, grants restored only because BOTH halves exist
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            with conn.cursor() as cur:
                pol = _rows(
                    cur,
                    "SELECT permission_key, param_scope, action FROM instance_permission_policies "
                    "WHERE permission_key LIKE 'storage.objects%'",
                )
                assert pol == sorted(
                    [(_OLD, None, "deny"), (_OLD, "scoped", "allow")], key=repr
                ), pol
                cur.execute(
                    "SELECT count(*) FROM community_permission_grants WHERE permission_id = %s",
                    (_OLD,),
                )
                assert cur.fetchone()[0] == 1  # type: ignore[index]
            alembic_cli("upgrade", "head", dsn=db.dsn)
        finally:
            conn.close()


@requires_docker
def test_downgrade_does_not_widen_a_half_granted_permission() -> None:
    with migrated_postgres("0119-halfgrant") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _skip_fk_enforcement(cur)
                cur.execute(
                    "INSERT INTO community_permission_grants "
                    "(community_id, tenant_id, app_id, permission_id) VALUES (1, 1, 'a.b.c', %s)",
                    (_READ,),
                )
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM community_permission_grants "
                    "WHERE permission_id LIKE 'storage.objects%'"
                )
                assert cur.fetchone()[0] == 0  # type: ignore[index]
        finally:
            conn.close()
