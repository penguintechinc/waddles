"""Real-Postgres tests for 0051_bundle_identity_resolve (bundle `identity` capability).

Runs the actual Alembic chain to `head` in an ephemeral container (see
`pg_docker.py`) and asserts the widened `community_member_identities` view:
the five pre-existing columns keep their order, the two new ones are
appended, NO PII column is projected, the active-membership predicate matches
the economy/reputation stores', and `waddles_bundle_reader` can read the view
but nothing underneath it -- none of which a mocked `op.execute` could verify.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "versions" / "0051_bundle_identity_resolve.py"
)
_READER = "waddles_bundle_reader"
_VIEW = "community_member_identities"
_USER_TRIGGER = "trg_community_members_user_uuid"

#: 0045's columns, in order, then the two this migration appends.
_EXPECTED_COLUMNS = [
    "community_id",
    "platform",
    "platform_user_id",
    "hub_user_uuid",
    "user_uuid",
    "tenant_id",
    "is_active_member",
]
_PII_COLUMNS = {
    "display_name",
    "username",
    "email",
    "avatar_url",
    "bio",
    "social_links",
    "platform_username",
    "password_hash",
    "user_id",
}


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0048", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_chains_off_0050_economy_store(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0051_bundle_identity_resolve"
        assert migration.down_revision == "0050_bundle_economy_store"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32

    def test_ddl_file_the_migration_loads_exists(self) -> None:
        assert _load_migration()._SQL_PATH.is_file()

    def test_ddl_adds_no_role_password_or_table_privilege(self) -> None:
        """The widened view must not need any new data-plane credential."""
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")
        ).upper()
        assert "CREATE ROLE" not in code
        assert "ALTER ROLE" not in code
        assert "PASSWORD" not in code
        # The only grant is the existing reader's SELECT on the (PII-free) view.
        grants = [
            ln.strip() for ln in code.splitlines() if ln.strip().startswith("GRANT")
        ]
        assert grants == [f"GRANT SELECT ON {_VIEW.upper()} TO {_READER.upper()};"]

    def test_ddl_view_projection_has_no_pii_column(self) -> None:
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        start = sql.index("CREATE OR REPLACE VIEW")
        projection = sql[start : sql.index("FROM community_members cm", start)]
        for pii in _PII_COLUMNS:
            assert f"cm.{pii}" not in projection, f"view must never project {pii}"
            assert f"hu.{pii}" not in projection, f"view must never project {pii}"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0051-identity") as db:
        yield db


def _connect(db: PgTestDatabase, **overrides: str):  # type: ignore[no-untyped-def]
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=overrides.get("user", db.user),
        password=overrides.get("password", db.password),
        dbname=db.dbname,
    )
    conn.autocommit = True
    return conn


@contextmanager
def _cursor(db: PgTestDatabase) -> Iterator[Any]:
    """One AUTOCOMMIT cursor (never `with conn`, which opens a transaction)."""
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


@contextmanager
def _auto_resolve_off(cur: Any) -> Iterator[None]:
    """Disable 0045's auto-resolving membership hook so a row's `user_uuid` is exactly what we set.

    In production that hook resolves every member carrying a platform id, so a NULL
    `user_uuid` only survives for an unresolvable row (no link, no platform id, or a second
    platform account of one hub user in a community) -- the shapes this fixture forces.
    """
    cur.execute(f"ALTER TABLE community_members DISABLE TRIGGER {_USER_TRIGGER}")
    try:
        yield
    finally:
        cur.execute(f"ALTER TABLE community_members ENABLE TRIGGER {_USER_TRIGGER}")


def _seed(cur: Any) -> dict[str, Any]:
    """One tenant pair, one community each, and members in every membership state."""
    tag = uuid.uuid4().hex[:12]
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s), (%s) RETURNING id",
        (f"id-t1-{tag}", f"id-t2-{tag}"),
    )
    t1, t2 = (r[0] for r in cur.fetchall())
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s), (%s, %s) RETURNING id",
        (f"c1-{tag}", t1, f"c2-{tag}", t2),
    )
    c1, c2 = (r[0] for r in cur.fetchall())
    uuids = {
        "active": "aaaaaaaa-0000-4000-8000-000000000001",
        "left": "aaaaaaaa-0000-4000-8000-000000000002",
        "removed": "aaaaaaaa-0000-4000-8000-000000000003",
        "inactive": "aaaaaaaa-0000-4000-8000-000000000004",
        "null_active": "aaaaaaaa-0000-4000-8000-000000000005",
    }
    rows = [
        ("active", "p-active", "true", "NULL", "NULL"),
        ("left", "p-left", "true", "now()", "NULL"),
        ("removed", "p-removed", "true", "NULL", "now()"),
        ("inactive", "p-inactive", "false", "NULL", "NULL"),
        ("null_active", "p-nullactive", "NULL", "NULL", "NULL"),
    ]
    with _auto_resolve_off(cur):
        for key, pid, is_active, left_at, removed_at in rows:
            cur.execute(
                "INSERT INTO community_members "
                "(community_id, platform, platform_user_id, display_name, is_active, left_at, "
                " removed_at, user_uuid) "
                f"VALUES (%s, 'twitch', %s, 'Secret Display Name', {is_active}, {left_at}, "
                f"{removed_at}, %s)",
                (c1, pid, uuids[key]),
            )
        # A member with NO resolved identity (user_uuid NULL): must read as such.
        cur.execute(
            "INSERT INTO community_members "
            "(community_id, platform, platform_user_id, is_active, user_uuid) "
            "VALUES (%s, 'twitch', 'p-unlinked', true, NULL)",
            (c1,),
        )
        # Same platform id in the OTHER tenant's community (cross-tenant fixture).
        cur.execute(
            "INSERT INTO community_members "
            "(community_id, platform, platform_user_id, is_active, user_uuid) "
            "VALUES (%s, 'twitch', 'p-active', true, 'bbbbbbbb-0000-4000-8000-000000000001')",
            (c2,),
        )
    return {"t1": t1, "t2": t2, "c1": c1, "c2": c2, "uuids": uuids}


@requires_docker
class TestViewShape:
    def test_columns_are_the_0045_five_in_order_then_the_two_appended(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (_VIEW,),
            )
            assert [r[0] for r in cur.fetchall()] == _EXPECTED_COLUMNS

    def test_no_pii_column_is_projected(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (_VIEW,),
            )
            columns = {r[0] for r in cur.fetchall()}
        assert columns.isdisjoint(_PII_COLUMNS), columns & _PII_COLUMNS

    def test_ddl_is_idempotent_a_second_application_succeeds(
        self, pg_db: PgTestDatabase
    ) -> None:
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        with _cursor(pg_db) as cur:
            cur.execute(sql)
            cur.execute(sql)


@requires_docker
class TestActiveMembershipPredicate:
    def test_is_active_member_matches_the_economy_and_reputation_predicate(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            seeded = _seed(cur)
            cur.execute(
                f"SELECT platform_user_id, is_active_member FROM {_VIEW} "
                "WHERE community_id = %s AND tenant_id = %s ORDER BY platform_user_id",
                (seeded["c1"], seeded["t1"]),
            )
            got = dict(cur.fetchall())
        assert got == {
            "p-active": True,
            "p-left": False,  # left_at set
            "p-removed": False,  # removed_at set
            "p-inactive": False,  # is_active = false
            "p-nullactive": False,  # NULL is_active is NOT active (fail-closed)
            "p-unlinked": True,  # active member whose identity is not resolved yet
        }

    def test_tenant_id_scopes_the_same_platform_id_apart(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            seeded = _seed(cur)
            cur.execute(
                f"SELECT tenant_id, user_uuid::text FROM {_VIEW} "
                "WHERE platform = 'twitch' AND platform_user_id = 'p-active' "
                "AND community_id IN (%s, %s) ORDER BY tenant_id",
                (seeded["c1"], seeded["c2"]),
            )
            rows = cur.fetchall()
        assert rows == [
            (seeded["t1"], seeded["uuids"]["active"]),
            (seeded["t2"], "bbbbbbbb-0000-4000-8000-000000000001"),
        ]

    def test_an_unresolved_identity_is_a_null_user_uuid_never_a_default(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            seeded = _seed(cur)
            cur.execute(
                f"SELECT user_uuid, is_active_member FROM {_VIEW} "
                "WHERE community_id = %s AND platform_user_id = 'p-unlinked'",
                (seeded["c1"],),
            )
            assert cur.fetchone() == (None, True)


@requires_docker
class TestDowngradeRoundTrip:
    def test_downgrade_restores_the_0045_view_shape_and_upgrade_reapplies(self) -> None:
        import os
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parents[2]
        with migrated_postgres("0051-roundtrip") as db:
            env = {**os.environ, "DATABASE_URL": db.dsn}
            env.setdefault("DB_READER_PASSWORD", "pg-docker-harness-default-reader-pw")

            def alembic(*args: str) -> None:
                subprocess.run(
                    [sys.executable, "-m", "alembic", *args],
                    cwd=repo_root,
                    env=env,
                    capture_output=True,
                    check=True,
                )

            def view_columns() -> list[str]:
                with _cursor(db) as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s ORDER BY ordinal_position",
                        (_VIEW,),
                    )
                    return [r[0] for r in cur.fetchall()]

            assert view_columns() == _EXPECTED_COLUMNS
            alembic("downgrade", "0050_bundle_economy_store")
            assert view_columns() == _EXPECTED_COLUMNS[:5]
            with _cursor(db) as cur:
                assert _priv(cur, _VIEW, "SELECT") is True, (
                    "downgrade must re-grant the reader"
                )
            alembic("upgrade", "head")
            assert view_columns() == _EXPECTED_COLUMNS


def _priv(cur: Any, table: str, priv: str) -> bool:
    cur.execute("SELECT has_table_privilege(%s, %s, %s)", (_READER, table, priv))
    return bool(cur.fetchone()[0])


@requires_docker
class TestReaderRoleBoundary:
    def test_reader_can_select_the_view_but_not_the_base_tables(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_READER,))
            assert cur.fetchone() == (1,), (
                "migration chain must provision the reader role"
            )
            assert _priv(cur, _VIEW, "SELECT") is True
            # The widened view must not widen the role's PII reach.
            # (`communities` is deliberately not asserted: the reader already
            # holds a pre-existing SELECT on it for the active-set queries.)
            assert _priv(cur, "community_members", "SELECT") is False
            assert _priv(cur, "ephemeral_pseudonyms", "SELECT") is False
            assert _priv(cur, "hub_users", "SELECT") is False

    def test_reader_cannot_select_a_pii_column_through_the_base_table(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            _seed(cur)
            cur.execute(f"SET ROLE {_READER}")
            try:
                cur.execute(
                    f"SELECT platform_user_id, user_uuid, tenant_id FROM {_VIEW} LIMIT 1"
                )
                assert cur.fetchone() is not None
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cur.execute("SELECT display_name FROM community_members LIMIT 1")
            finally:
                cur.execute("RESET ROLE")
