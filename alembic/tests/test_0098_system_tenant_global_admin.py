"""Real-Postgres tests for 0098_system_tenant_global_admin.

Covers: super admins (and the bootstrap admin) become `system` tenant members + admins and the
`is_super_admin` column is dropped; the catch-all tenant `global` is renamed `default` KEEPING its
id so FKs follow; both-slugs-present fails loud; downgrade restores the flag from system admins and
reverses the rename; the whole thing is idempotent on re-run.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
    PgTestDatabase,
    alembic_cli,
    migrated_postgres,
)

_MIGRATION = REPO_ROOT / "alembic" / "versions" / "0098_system_tenant_global_admin.py"
_REVISION = "0098_system_tenant_global_admin"


def _previous_revision() -> str:
    """The revision 0098 chains after (read from the file so a re-chain needs no test edit)."""
    spec = importlib.util.spec_from_file_location("mig_0098", Path(_MIGRATION))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.down_revision)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0098)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0098-system-tenant") as db:
        yield db


def _q(
    db: PgTestDatabase, sql: str, args: tuple[object, ...] = ()
) -> list[tuple[object, ...]]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def _reset_to_previous(db: PgTestDatabase) -> None:
    """Downgrade to the pre-0098 revision, then clear fixture rows."""
    alembic_cli("downgrade", _previous_revision(), dsn=db.dsn)
    _q(db, "DELETE FROM tenant_admins")
    _q(db, "DELETE FROM tenant_members")
    _q(db, "DELETE FROM communities")
    _q(db, "DELETE FROM hub_users")
    _q(db, "DELETE FROM tenants")


def test_shape_at_head(pg_db: PgTestDatabase) -> None:
    cols = _q(
        pg_db,
        "SELECT column_name FROM information_schema.columns WHERE table_name='hub_users'",
    )
    assert ("is_super_admin",) not in cols
    members = _q(
        pg_db,
        "SELECT column_name FROM information_schema.columns WHERE table_name='tenant_members'",
    )
    assert {c for (c,) in members} >= {"tenant_id", "user_id", "created_at"}


def test_upgrade_moves_super_admins_renames_default_and_drops_column(
    pg_db: PgTestDatabase,
) -> None:
    _reset_to_previous(pg_db)
    # Pre-0098 world: legacy flag column, 'global' catch-all tenant with a community FK.
    _q(
        pg_db,
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN DEFAULT FALSE",
    )
    _q(
        pg_db,
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_bootstrap BOOLEAN DEFAULT FALSE",
    )
    (gid,) = _q(
        pg_db,
        "INSERT INTO tenants (slug, is_global) VALUES ('global', TRUE) RETURNING id",
    )[0]
    _q(pg_db, "INSERT INTO communities (name, tenant_id) VALUES ('c1', %s)", (gid,))
    _q(
        pg_db,
        "INSERT INTO hub_users (username, is_super_admin, is_bootstrap) VALUES "
        "('boot', true, true), ('sa2', true, false), ('plain', false, false)",
    )

    alembic_cli("upgrade", _REVISION, dsn=pg_db.dsn)

    # Rename kept the id; the FK still resolves through it.
    assert _q(pg_db, "SELECT id FROM tenants WHERE slug='default'") == [(gid,)]
    assert _q(pg_db, "SELECT count(*) FROM tenants WHERE slug='global'") == [(0,)]
    assert _q(
        pg_db,
        "SELECT c.name FROM communities c JOIN tenants t ON t.id=c.tenant_id WHERE t.slug='default'",
    ) == [("c1",)]
    # system tenant exists; super admins + bootstrap are members AND tenant admins of it.
    (sid,) = _q(pg_db, "SELECT id FROM tenants WHERE slug='system'")[0]
    admins = _q(
        pg_db,
        "SELECT u.username FROM tenant_admins ta JOIN hub_users u ON u.id=ta.user_id "
        "WHERE ta.tenant_id=%s ORDER BY u.username",
        (sid,),
    )
    assert admins == [("boot",), ("sa2",)]
    members = _q(
        pg_db,
        "SELECT u.username FROM tenant_members tm JOIN hub_users u ON u.id=tm.user_id "
        "WHERE tm.tenant_id=%s ORDER BY u.username",
        (sid,),
    )
    assert members == [("boot",), ("sa2",)]
    cols = _q(
        pg_db,
        "SELECT column_name FROM information_schema.columns WHERE table_name='hub_users'",
    )
    assert ("is_super_admin",) not in cols


def test_downgrade_restores_flag_from_system_admins_and_reverses_rename(
    pg_db: PgTestDatabase,
) -> None:
    # State from the previous test is at 0098: boot + sa2 are system admins, 'default' is renamed.
    (gid,) = _q(pg_db, "SELECT id FROM tenants WHERE slug='default'")[0]
    alembic_cli("downgrade", _previous_revision(), dsn=pg_db.dsn)
    flagged = _q(
        pg_db, "SELECT username FROM hub_users WHERE is_super_admin ORDER BY username"
    )
    assert flagged == [("boot",), ("sa2",)]
    assert _q(pg_db, "SELECT id FROM tenants WHERE slug='global'") == [(gid,)]
    # Round trip: upgrade again is clean.
    alembic_cli("upgrade", _REVISION, dsn=pg_db.dsn)
    assert _q(pg_db, "SELECT id FROM tenants WHERE slug='default'") == [(gid,)]


def test_upgrade_is_idempotent_when_default_already_exists(
    pg_db: PgTestDatabase,
) -> None:
    _reset_to_previous(pg_db)
    (did,) = _q(pg_db, "INSERT INTO tenants (slug) VALUES ('default') RETURNING id")[0]
    alembic_cli("upgrade", _REVISION, dsn=pg_db.dsn)
    assert _q(pg_db, "SELECT id FROM tenants WHERE slug='default'") == [(did,)]
    assert _q(pg_db, "SELECT count(*) FROM tenants WHERE slug='system'") == [(1,)]


def test_both_global_and_default_present_fails_loud(pg_db: PgTestDatabase) -> None:
    _reset_to_previous(pg_db)
    _q(pg_db, "INSERT INTO tenants (slug) VALUES ('global'), ('default')")
    with pytest.raises(RuntimeError, match="already holds that slug"):
        alembic_cli("upgrade", _REVISION, dsn=pg_db.dsn)
    # Nothing half-applied: both rows untouched.
    assert {s for (s,) in _q(pg_db, "SELECT slug FROM tenants")} == {
        "global",
        "default",
    }


def test_preexisting_system_tenant_with_non_super_admin_admins_fails_loud(
    pg_db: PgTestDatabase,
) -> None:
    """A `system` tenant that already has a plain user as admin must not silently promote them."""
    _reset_to_previous(pg_db)
    _q(
        pg_db,
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN DEFAULT FALSE",
    )
    _q(
        pg_db,
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_bootstrap BOOLEAN DEFAULT FALSE",
    )
    (sid,) = _q(pg_db, "INSERT INTO tenants (slug) VALUES ('system') RETURNING id")[0]
    (real,) = _q(
        pg_db,
        "INSERT INTO hub_users (username, is_super_admin) VALUES ('sa', true) RETURNING id",
    )[0]
    (plain,) = _q(
        pg_db,
        "INSERT INTO hub_users (username, is_super_admin) VALUES ('plain', false) RETURNING id",
    )[0]
    _q(
        pg_db,
        "INSERT INTO tenant_admins (tenant_id, user_id) VALUES (%s, %s), (%s, %s)",
        (sid, real, sid, plain),
    )
    with pytest.raises(RuntimeError, match="silently become global admins") as info:
        alembic_cli("upgrade", _REVISION, dsn=pg_db.dsn)
    assert str(plain) in str(info.value)
    # Nothing promoted, flag column still present (transactional DDL rolled back).
    assert _q(pg_db, "SELECT count(*) FROM tenant_members") == [(0,)]
    cols = _q(
        pg_db,
        "SELECT column_name FROM information_schema.columns WHERE table_name='hub_users'",
    )
    assert ("is_super_admin",) in cols


def test_preexisting_system_tenant_with_only_super_admin_admins_upgrades(
    pg_db: PgTestDatabase,
) -> None:
    """The legitimate re-run shape (system admins are all super admins) still upgrades."""
    _reset_to_previous(pg_db)
    _q(
        pg_db,
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN DEFAULT FALSE",
    )
    _q(
        pg_db,
        "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_bootstrap BOOLEAN DEFAULT FALSE",
    )
    (sid,) = _q(pg_db, "INSERT INTO tenants (slug) VALUES ('system') RETURNING id")[0]
    (real,) = _q(
        pg_db,
        "INSERT INTO hub_users (username, is_super_admin) VALUES ('sa', true) RETURNING id",
    )[0]
    _q(
        pg_db,
        "INSERT INTO tenant_admins (tenant_id, user_id) VALUES (%s, %s)",
        (sid, real),
    )
    alembic_cli("upgrade", _REVISION, dsn=pg_db.dsn)
    assert _q(
        pg_db, "SELECT user_id FROM tenant_admins WHERE tenant_id=%s", (sid,)
    ) == [(real,)]
