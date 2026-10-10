"""Tests for 0058_provider_connections (provider/connection framework columns).

Two layers, both cheap to keep green:

* a static schema-drift guard (no database): the columns the migration adds must equal the columns
  the SQLAlchemy models gained, so ``PlatformConnection`` / ``CommunityConnectionAccess`` cannot
  silently diverge from the migration (same methodology as ``test_0035_schema_drift.py``);
* real-Postgres tests (``pg_docker.migrated_postgres``, replays 0020 -> head): columns, defaults,
  constraints and the index exist; pre-existing 0035-shaped rows survive with the right defaults;
  the migration changes no privilege (the data plane still cannot read a token column); and
  ``downgrade`` then ``upgrade`` round-trips.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0058_provider_connections.py"
)
_PREVIOUS_REVISION = "0057_communities_overlay_code"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0058_provider_connections", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    with patch("alembic.op.execute") as mock_execute:
        _load_migration().upgrade()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


def _added_columns(sql: str, table: str) -> set[str]:
    """Column names added to ``table`` by ``ALTER TABLE <table> ADD COLUMN IF NOT EXISTS``."""
    columns: set[str] = set()
    for statement in re.findall(
        rf"ALTER TABLE {table}\s+(.*?)(?=ALTER TABLE|CREATE INDEX|COMMENT ON|\Z)",
        sql,
        flags=re.S,
    ):
        columns.update(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", statement))
    return columns


class TestStaticSchemaDrift:
    def test_the_revision_chain_is_what_the_docstring_says(self) -> None:
        module = _load_migration()
        assert module.revision == "0058_provider_connections"
        assert module.down_revision == _PREVIOUS_REVISION
        assert len(module.revision) <= 32

    def test_columns_added_equal_the_model_columns_that_are_new(self) -> None:
        root = Path(__file__).resolve().parents[2] / "libs" / "flask_core"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from flask_core.models import guild_pairing  # type: ignore[import-not-found]

        sql = _upgrade_sql()
        pre_0058 = {
            "platform_connections": {
                "id",
                "tenant_id",
                "platform",
                "resource_type",
                "resource_id",
                "access_token",
                "refresh_token",
                "status",
                "installed_by_user_id",
                "created_at",
                "updated_at",
            },
            "community_connection_access": {
                "id",
                "community_id",
                "connection_id",
                "status",
                "requested_by_user_id",
                "approved_by_user_id",
                "created_at",
                "updated_at",
            },
        }
        models = {
            "platform_connections": guild_pairing.PlatformConnection,
            "community_connection_access": guild_pairing.CommunityConnectionAccess,
        }
        for table, model in models.items():
            model_new = {c.name for c in model.__table__.columns} - pre_0058[table]
            sql_new = _added_columns(sql, table)
            assert sql_new, f"no ADD COLUMN found for {table}"
            assert sql_new == model_new, table

    def test_the_0035_drift_guard_knows_exactly_the_columns_0058_adds(self) -> None:
        from test_0035_schema_drift import _COLUMNS_ADDED_LATER  # type: ignore[import-not-found]

        sql = _upgrade_sql()
        for table, columns in _COLUMNS_ADDED_LATER.items():
            assert _added_columns(sql, table) == columns, table

    def test_downgrade_drops_everything_upgrade_adds(self) -> None:
        with patch("alembic.op.execute") as mock_execute:
            _load_migration().downgrade()
        down = "\n".join(call.args[0] for call in mock_execute.call_args_list)
        for name in (
            "resource_metadata",
            "expires_at",
            "status_reason",
            "enabled",
            "idx_platform_connections_platform_status",
            "platform_connections_status_reason_check",
        ):
            assert name in down, name

    def test_the_resource_type_allow_list_is_left_alone(self) -> None:
        assert "resource_type" not in _upgrade_sql().replace("resource_type_", "")


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant(cur: psycopg2.extensions.cursor, slug: str) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
    return int(cur.fetchone()[0])


def _community(cur: psycopg2.extensions.cursor, name: str) -> int:
    cur.execute("INSERT INTO communities (name) VALUES (%s) RETURNING id", (name,))
    return int(cur.fetchone()[0])


def _connection(cur: psycopg2.extensions.cursor, tenant_id: int, resource_id: str = "r1") -> int:
    """A row written exactly as a pre-0058 (0035-era) caller would: new columns omitted."""
    cur.execute(
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, access_token) "
        "VALUES (%s, 'twitch', 'twitch_channel', %s, 'ciphertext') RETURNING id",
        (tenant_id, resource_id),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0058)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0058-provider-connections") as db:
        yield db


@requires_docker
class TestColumnsAndDefaults:
    def test_new_columns_exist_with_the_documented_types(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_name IN "
                    "('platform_connections','community_connection_access') "
                    "AND column_name IN ('resource_metadata','expires_at','status_reason','enabled')"
                )
                cols = {(t, c): (d, n, default) for t, c, d, n, default in cur.fetchall()}
        finally:
            conn.close()
        assert len(cols) == 4
        meta = cols[("platform_connections", "resource_metadata")]
        assert (meta[0], meta[1]) == ("jsonb", "NO") and "'{}'" in meta[2]
        assert cols[("platform_connections", "expires_at")][:2] == (
            "timestamp with time zone",
            "YES",
        )
        assert cols[("platform_connections", "status_reason")][:2] == (
            "character varying",
            "YES",
        )
        enabled = cols[("community_connection_access", "enabled")]
        assert (enabled[0], enabled[1]) == ("boolean", "NO") and "true" in enabled[2].lower()

    def test_a_pre_0058_style_insert_gets_safe_defaults(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-defaults")
                community = _community(cur, "c-defaults")
                cid = _connection(cur, tenant)
                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id) VALUES (%s, %s)",
                    (community, cid),
                )
                cur.execute(
                    "SELECT resource_metadata, expires_at, status_reason FROM platform_connections WHERE id=%s",
                    (cid,),
                )
                assert cur.fetchone() == ({}, None, None)
                cur.execute(
                    "SELECT enabled, status FROM community_connection_access WHERE connection_id=%s",
                    (cid,),
                )
                assert cur.fetchone() == (
                    True,
                    "pending",
                )  # existing grants keep behaving as before
        finally:
            conn.close()

    def test_status_reason_must_be_a_machine_code(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid = _connection(cur, _tenant(cur, "t-reason"))
                cur.execute(
                    "UPDATE platform_connections SET status_reason='refresh_rejected' WHERE id=%s",
                    (cid,),
                )
                cur.execute(
                    "UPDATE platform_connections SET status_reason=NULL WHERE id=%s",
                    (cid,),
                )
            for bad in ("Refresh Rejected", "1abc", "has-dash", "x" * 41):
                with (
                    pytest.raises(
                        (
                            psycopg2.errors.CheckViolation,
                            psycopg2.errors.StringDataRightTruncation,
                        )
                    ),
                    conn.cursor() as cur,
                ):
                    cur.execute(
                        "UPDATE platform_connections SET status_reason=%s WHERE id=%s",
                        (bad, cid),
                    )
        finally:
            conn.close()

    def test_resource_metadata_roundtrips_json_and_rejects_null(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid = _connection(cur, _tenant(cur, "t-json"))
                cur.execute(
                    "UPDATE platform_connections SET resource_metadata=%s::jsonb WHERE id=%s",
                    ('{"channel_login": "x", "scopes": ["chat:read"]}', cid),
                )
                cur.execute(
                    "SELECT resource_metadata->>'channel_login', resource_metadata->'scopes' FROM platform_connections WHERE id=%s",
                    (cid,),
                )
                assert cur.fetchone() == ("x", ["chat:read"])
            with pytest.raises(psycopg2.errors.NotNullViolation), conn.cursor() as cur:
                cur.execute(
                    "UPDATE platform_connections SET resource_metadata=NULL WHERE id=%s",
                    (cid,),
                )
        finally:
            conn.close()

    def test_the_data_plane_lookup_index_exists(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT indexdef FROM pg_indexes WHERE indexname='idx_platform_connections_platform_status'"
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None and "(platform, status)" in row[0]

    def test_the_existing_resource_type_allow_list_still_holds(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-allow")
            with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, access_token) "
                    "VALUES (%s,'x','slack_workspace','w1','c')",
                    (tenant,),
                )
        finally:
            conn.close()


@requires_docker
class TestPrivilegesUnchanged:
    def test_no_data_plane_role_can_read_a_token_or_the_new_columns(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT grantee FROM information_schema.role_table_grants "
                    "WHERE table_name IN ('platform_connections','community_connection_access') "
                    "AND grantee <> current_user"
                )
                grantees = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        assert grantees <= {"hub_api", "migration_runner"}, grantees
        assert "PUBLIC" not in grantees


@requires_docker
class TestRoundTrip:
    def test_downgrade_then_upgrade_preserves_the_0035_data(self) -> None:
        with migrated_postgres("0058-roundtrip") as db:
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    tenant = _tenant(cur, "t-rt")
                    cid = _connection(cur, tenant)
                    cur.execute(
                        "UPDATE platform_connections SET resource_metadata='{\"k\": 1}'::jsonb WHERE id=%s",
                        (cid,),
                    )
            finally:
                conn.close()

            alembic_cli("downgrade", _PREVIOUS_REVISION, dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name IN "
                        "('platform_connections','community_connection_access') AND column_name IN "
                        "('resource_metadata','expires_at','status_reason','enabled')"
                    )
                    assert cur.fetchall() == [], "downgrade left a column behind"
                    cur.execute("SELECT to_regclass('idx_platform_connections_platform_status')")
                    assert cur.fetchone() == (None,)
                    cur.execute(
                        "SELECT access_token FROM platform_connections WHERE id=%s",
                        (cid,),
                    )
                    assert cur.fetchone() == (
                        "ciphertext",
                    ), "downgrade must never touch the tokens"
            finally:
                conn.close()

            alembic_cli("upgrade", "head", dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT resource_metadata, access_token FROM platform_connections WHERE id=%s",
                        (cid,),
                    )
                    assert cur.fetchone() == (
                        {},
                        "ciphertext",
                    )  # re-added with defaults; token intact
            finally:
                conn.close()
