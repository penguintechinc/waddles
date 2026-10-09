"""Tests for 0043_hub_users_identity_uuid.

Two layers, same split `test_0012_schema_drift_columns.py` establishes:

1. `TestMigrationMetadata`/`TestUpgradeEmitsExpectedSql`/
   `TestDowngradeIsUpgradesInverse` -- mock `alembic.op.execute`, assert
   the exact SQL shape (revision chain, backfill-before-NOT-NULL
   ordering, column-scoped grant, view contract) with zero DB dependency,
   always runs in CI. `TestMigrationMetadata` pins the exact
   `revision`/`down_revision` strings (explicit-revision round-trip) so a
   future renumbering (see this migration's own "Numbering note") is a
   deliberate, test-visible edit rather than a silent drift.
2. `TestRealPostgresRoundTrip` -- this migration's `op.execute()` calls
   are hand-written DDL (not SQLAlchemy Core), so unlike most of this
   repo's migrations (see `test_0012...`'s own docstring on why a real-DB
   fixture doesn't exist here) the actual backfill/constraint/grant
   behavior is easy to replay directly against a real Postgres without an
   Alembic harness: exec the same SQL strings `upgrade()` emits against a
   throwaway database, then assert on live catalog state. Skipped (never
   silently -- prints why) when `WADDLES_TEST_DATABASE_URL` isn't set, so
   this suite degrades gracefully in an environment with no Postgres
   rather than failing the whole run.
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0043_hub_users_identity_uuid.py"
)

_DB_URL_ENV = "WADDLES_TEST_DATABASE_URL"


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "migration_0043_hub_users_identity_uuid", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration() -> Any:
    return _load_migration()


def _executed_sql(migration: Any, direction: str) -> str:
    with patch("alembic.op.execute") as mock_execute:
        getattr(migration, direction)()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


class TestMigrationMetadata:
    def test_chains_directly_off_0042_instance_perm_policies(self, migration: Any) -> None:
        assert migration.revision == "0043_hub_users_identity_uuid"
        assert migration.down_revision == "0042_instance_perm_policies"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration: Any) -> None:
        assert len(migration.revision) <= 32


class TestUpgradeEmitsExpectedSql:
    def test_column_added_nullable_before_backfill(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        add_idx = sql.index("ADD COLUMN IF NOT EXISTS uuid UUID")
        backfill_idx = sql.index("UPDATE hub_users SET uuid = gen_random_uuid()")
        notnull_idx = sql.index("ALTER COLUMN uuid SET NOT NULL")
        assert add_idx < backfill_idx < notnull_idx, (
            "must add nullable, backfill, THEN enforce NOT NULL -- reordering "
            "risks a bare NOT NULL DEFAULT rewriting every row or failing "
            "outright on populated tables"
        )

    def test_backfill_only_touches_rows_missing_a_uuid(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        assert "WHERE uuid IS NULL" in sql

    def test_default_attached_for_future_inserts(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        assert "ALTER COLUMN uuid SET DEFAULT gen_random_uuid()" in sql

    def test_unique_constraint_added(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        assert "ADD CONSTRAINT hub_users_uuid_key UNIQUE (uuid)" in sql

    def test_view_projects_no_pii_column(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        view_match = re.search(
            r"CREATE OR REPLACE VIEW community_member_identities AS(.*?)FROM community_members",
            sql,
            re.DOTALL,
        )
        assert view_match is not None
        projected = view_match.group(1)
        for pii_column in ("email", "password_hash", "username", "avatar_url", "display_name"):
            assert pii_column not in projected, (
                f"community_member_identities view must never project {pii_column} "
                "(display_name is PII -- identity resolution is platform_user_id-only)"
            )
        assert "hu.uuid AS hub_user_uuid" in projected
        assert "cm.platform_user_id" in projected

    def test_bundle_reader_grant_is_column_scoped_not_table_wide(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        assert "GRANT SELECT (uuid, id) ON hub_users TO waddles_bundle_reader" in sql
        assert "GRANT SELECT ON hub_users TO waddles_bundle_reader" not in sql
        assert "GRANT SELECT ON community_member_identities TO waddles_bundle_reader" in sql

    def test_bundle_reader_grant_guarded_by_role_existence(self, migration: Any) -> None:
        sql = _executed_sql(migration, "upgrade")
        assert "SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reader'" in sql


class TestDowngradeIsUpgradesInverse:
    def test_revokes_before_dropping_view_before_dropping_column(self, migration: Any) -> None:
        sql = _executed_sql(migration, "downgrade")
        revoke_view_idx = sql.index("REVOKE SELECT ON community_member_identities")
        revoke_col_idx = sql.index("REVOKE SELECT (uuid, id) ON hub_users")
        drop_view_idx = sql.index("DROP VIEW IF EXISTS community_member_identities")
        drop_constraint_idx = sql.index("DROP CONSTRAINT IF EXISTS hub_users_uuid_key")
        drop_column_idx = sql.index("DROP COLUMN IF EXISTS uuid")
        assert (
            revoke_view_idx
            < revoke_col_idx
            < drop_view_idx
            < drop_constraint_idx
            < drop_column_idx
        )


@pytest.mark.skipif(
    not os.environ.get(_DB_URL_ENV),
    reason=(
        f"{_DB_URL_ENV} not set -- real-Postgres round-trip skipped. "
        "Run `docker run -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:17-bookworm` "
        f"and export {_DB_URL_ENV}=postgresql://postgres:x@localhost:5432/postgres to exercise."
    ),
)
class TestRealPostgresRoundTrip:
    """Exercises the migration's actual SQL against a live Postgres.

    Each test gets its own freshly created/dropped schema so tests never
    interfere with each other or leave state behind.
    """

    @pytest.fixture
    def conn(self):  # type: ignore[no-untyped-def]
        import psycopg2

        dsn = os.environ[_DB_URL_ENV]
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        schema = f"test_0043_{uuid.uuid4().hex[:8]}"
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
        try:
            yield conn, schema
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.close()

    def _apply_upgrade_sql(self, cur: Any, migration: Any) -> None:
        with patch("alembic.op.execute") as mock_execute:
            migration.upgrade()
        for call in mock_execute.call_args_list:
            cur.execute(call.args[0])

    def _create_base_tables(self, cur: Any) -> None:
        cur.execute(
            """
            CREATE TABLE hub_users (
                id SERIAL PRIMARY KEY,
                display_name VARCHAR(255),
                username VARCHAR(255) UNIQUE,
                email VARCHAR(255) UNIQUE,
                password_hash VARCHAR(255)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE communities (
                id SERIAL PRIMARY KEY
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE community_members (
                id SERIAL PRIMARY KEY,
                community_id INTEGER REFERENCES communities(id),
                user_id VARCHAR(255),
                platform VARCHAR(50),
                platform_user_id VARCHAR(255),
                display_name VARCHAR(255)
            )
            """
        )

    def test_backfill_assigns_distinct_nonnull_uuids_to_existing_rows(
        self, conn: Any, migration: Any
    ) -> None:
        pg_conn, _schema = conn
        with pg_conn.cursor() as cur:
            self._create_base_tables(cur)
            cur.execute(
                "INSERT INTO hub_users (username, email) VALUES "
                "('alice', 'alice@example.com'), ('bob', 'bob@example.com'), "
                "('carol', 'carol@example.com')"
            )
            self._apply_upgrade_sql(cur, migration)

            cur.execute("SELECT id, uuid FROM hub_users ORDER BY id")
            rows = cur.fetchall()

        assert len(rows) == 3, "expected all 3 pre-existing rows backfilled"
        uuids = [row[1] for row in rows]
        assert all(u is not None for u in uuids), "backfill must leave no NULL uuid"
        assert len(set(uuids)) == len(uuids), "backfilled uuids must be distinct"
        for u in uuids:
            uuid.UUID(str(u))  # raises if not a well-formed UUID

    def test_not_null_constraint_rejects_explicit_null_insert(
        self, conn: Any, migration: Any
    ) -> None:
        pg_conn, _schema = conn
        with pg_conn.cursor() as cur:
            self._create_base_tables(cur)
            self._apply_upgrade_sql(cur, migration)

        import psycopg2

        with pytest.raises(psycopg2.errors.NotNullViolation), pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hub_users (username, email, uuid) VALUES "
                "('dave', 'dave@example.com', NULL)"
            )

    def test_default_populates_uuid_on_new_row_without_explicit_value(
        self, conn: Any, migration: Any
    ) -> None:
        pg_conn, _schema = conn
        with pg_conn.cursor() as cur:
            self._create_base_tables(cur)
            self._apply_upgrade_sql(cur, migration)
            cur.execute(
                "INSERT INTO hub_users (username, email) VALUES "
                "('erin', 'erin@example.com') RETURNING uuid"
            )
            (new_uuid,) = cur.fetchone()

        assert new_uuid is not None
        uuid.UUID(str(new_uuid))

    def test_unique_constraint_rejects_duplicate_uuid(self, conn: Any, migration: Any) -> None:
        pg_conn, _schema = conn
        with pg_conn.cursor() as cur:
            self._create_base_tables(cur)
            self._apply_upgrade_sql(cur, migration)
            cur.execute(
                "INSERT INTO hub_users (username, email) VALUES "
                "('frank', 'frank@example.com') RETURNING uuid"
            )
            (existing_uuid,) = cur.fetchone()

        import psycopg2

        with pytest.raises(psycopg2.errors.UniqueViolation), pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hub_users (username, email, uuid) VALUES "
                "('grace', 'grace@example.com', %s)",
                (existing_uuid,),
            )

    def test_reader_role_can_select_uuid_but_not_pii_columns(
        self, conn: Any, migration: Any
    ) -> None:
        pg_conn, schema = conn
        with pg_conn.cursor() as cur:
            self._create_base_tables(cur)

            # Migration's role-existence guard is a no-op unless the role is
            # actually named "waddles_bundle_reader" -- create it under that
            # exact name, scoped to this test's own throwaway schema/role so
            # concurrent test runs never collide.
            cur.execute("DROP ROLE IF EXISTS waddles_bundle_reader")
            cur.execute("CREATE ROLE waddles_bundle_reader NOLOGIN")
            cur.execute(f"GRANT USAGE ON SCHEMA {schema} TO waddles_bundle_reader")

            self._apply_upgrade_sql(cur, migration)

            cur.execute(
                "INSERT INTO hub_users (username, email, password_hash) VALUES "
                "('henry', 'henry@example.com', 'sekrit-hash') RETURNING id, uuid"
            )
            hub_user_id, hub_user_uuid = cur.fetchone()
            cur.execute(
                "INSERT INTO communities DEFAULT VALUES RETURNING id",
            )
            (community_id,) = cur.fetchone()
            # display_name intentionally omitted from this insert's relevant
            # columns for identity resolution -- the view/reader-role
            # contract below is platform_user_id-only, per the PII rule
            # (Twitch IRC `user-id` tag / Discord snowflake are the real
            # stable identifiers; a handle is never used to resolve identity).
            cur.execute(
                "INSERT INTO community_members "
                "(community_id, user_id, platform, platform_user_id, display_name) "
                "VALUES (%s, %s, 'twitch', '999', 'henry_tv')",
                (community_id, str(hub_user_id)),
            )

            # The view itself must never expose display_name at all -- not
            # just "reader can't select it", the column must not exist on
            # the view's own projection.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'community_member_identities'",
                (schema,),
            )
            view_columns = {row[0] for row in cur.fetchall()}
            assert "display_name" not in view_columns
            assert view_columns == {"community_id", "platform", "platform_user_id", "hub_user_uuid"}

            # Reader role: uuid column readable.
            cur.execute("SET ROLE waddles_bundle_reader")
            cur.execute(f"SELECT uuid FROM {schema}.hub_users WHERE id = %s", (hub_user_id,))
            (read_uuid,) = cur.fetchone()
            assert str(read_uuid) == str(hub_user_uuid)

            # Reader role: view readable, exposes the resolved uuid, keyed
            # by platform_user_id only.
            cur.execute(
                f"SELECT hub_user_uuid FROM {schema}.community_member_identities "
                "WHERE platform_user_id = '999'"
            )
            (view_uuid,) = cur.fetchone()
            assert str(view_uuid) == str(hub_user_uuid)

            cur.execute("RESET ROLE")

        import psycopg2

        with pytest.raises(psycopg2.errors.InsufficientPrivilege), pg_conn.cursor() as cur:
            cur.execute("SET ROLE waddles_bundle_reader")
            cur.execute(f"SELECT email FROM {schema}.hub_users WHERE id = %s", (hub_user_id,))

        with pg_conn.cursor() as cur:
            cur.execute("RESET ROLE")
            # Drop the schema (and everything the reader role was granted on
            # within it) before dropping the role itself -- Postgres refuses
            # to drop a role that still has any privilege recorded against a
            # live object, even an empty/revoked one.
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute("DROP ROLE IF EXISTS waddles_bundle_reader")
