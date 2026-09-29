"""Real-Postgres verification of migration 0035's RBAC grants (security review HIGH, PR #442).

Applies the migration's actual `upgrade()` SQL (captured the same way
`alembic/tests/test_0036_keystore_tenant_dek.py` does -- patch
`alembic.op.execute`, replay the captured statements) against a live
Postgres, then asserts via `has_table_privilege()` that:

- `hub_api_keystore` (the dedicated role) can SELECT/INSERT/UPDATE both
  keystore tables.
- Every data-plane role (`svc_ingest`, `svc_process`, `svc_action`,
  `svc_streaming`, `webui`, `migration_runner`) -- **and plain `hub_api`
  itself** -- is denied on both tables.

Requires a real Postgres reachable at `TEST_KEYSTORE_DATABASE_URL`
(superuser/owner DSN, e.g. `postgres://waddlebot:testpass123@127.0.0.1:PORT/waddlebot`).
Skipped (not failed) when that env var is unset -- this repo has no
CI-wired live-Postgres fixture yet (see the alembic test file's own
docstring), so this is opt-in, run explicitly by whoever needs the live
guarantee (this PR's author ran it against an ephemeral
`postgres:17-bookworm` container as part of PR #442's security-review
remediation).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_KEYSTORE_DATABASE_URL"),
    reason="TEST_KEYSTORE_DATABASE_URL not set -- no live Postgres wired for this opt-in check",
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0036_keystore_tenant_dek.py"
)

_DATA_PLANE_ROLES = (
    "hub_api",
    "waddles_publisher",
    "svc_ingest",
    "svc_process",
    "svc_action",
    "svc_streaming",
    "webui",
    "migration_runner",
)

_KEYSTORE_TABLES = ("keystore.tenant_encryption_keys", "keystore.key_tombstones")


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("migration_0035_live", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_statements() -> list[str]:
    migration = _load_migration()
    with patch("alembic.op.execute") as mock_execute:
        migration.upgrade()
    return [call.args[0] for call in mock_execute.call_args_list]


@pytest.fixture
async def live_conn() -> Any:
    """A fresh connection to a throwaway `keystore_rbac_live_test` DB, dropped after the test."""
    admin_dsn = os.environ["TEST_KEYSTORE_DATABASE_URL"]
    admin_conn = await asyncpg.connect(admin_dsn)
    db_name = "keystore_rbac_live_test"
    try:
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        await admin_conn.execute(f"CREATE DATABASE {db_name}")
    finally:
        await admin_conn.close()

    # Reconnect to the fresh DB to apply the migration + run assertions.
    base = admin_dsn.rsplit("/", 1)[0]
    test_dsn = f"{base}/{db_name}"
    conn = await asyncpg.connect(test_dsn)
    try:
        for statement in _upgrade_statements():
            await conn.execute(statement)
        yield conn
    finally:
        await conn.close()
        admin_conn = await asyncpg.connect(admin_dsn)
        try:
            await admin_conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin_conn.close()


class TestKeystoreRbacOnRealPostgres:
    async def test_dedicated_role_can_select_insert_update_tenant_encryption_keys(
        self, live_conn: Any
    ) -> None:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            allowed = await live_conn.fetchval(
                "SELECT has_table_privilege('hub_api_keystore', "
                "'keystore.tenant_encryption_keys', $1)",
                priv,
            )
            assert allowed is True, f"hub_api_keystore missing {priv} on tenant_encryption_keys"

    async def test_dedicated_role_can_select_insert_tombstones_only(self, live_conn: Any) -> None:
        """`key_tombstones` is append-only -- SELECT/INSERT, no UPDATE, for anyone."""
        for priv in ("SELECT", "INSERT"):
            allowed = await live_conn.fetchval(
                "SELECT has_table_privilege('hub_api_keystore', 'keystore.key_tombstones', $1)",
                priv,
            )
            assert allowed is True, f"hub_api_keystore missing {priv} on key_tombstones"
        no_update = await live_conn.fetchval(
            "SELECT has_table_privilege('hub_api_keystore', 'keystore.key_tombstones', 'UPDATE')"
        )
        assert no_update is False, "key_tombstones must stay append-only, even for hub_api_keystore"

    async def test_dedicated_role_has_no_delete(self, live_conn: Any) -> None:
        for table in _KEYSTORE_TABLES:
            allowed = await live_conn.fetchval(
                "SELECT has_table_privilege('hub_api_keystore', $1, 'DELETE')", table
            )
            assert allowed is False, f"hub_api_keystore must never get DELETE on {table}"

    @pytest.mark.parametrize("role", _DATA_PLANE_ROLES)
    @pytest.mark.parametrize("table", _KEYSTORE_TABLES)
    async def test_data_plane_roles_denied(self, live_conn: Any, role: str, table: str) -> None:
        """Every non-dedicated role, INCLUDING plain `hub_api` itself, is denied."""
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            allowed = await live_conn.fetchval(
                "SELECT has_table_privilege($1, $2, $3)", role, table, priv
            )
            assert allowed is False, f"{role} must be denied {priv} on {table}"

    async def test_public_has_no_access(self, live_conn: Any) -> None:
        for table in _KEYSTORE_TABLES:
            allowed = await live_conn.fetchval(
                "SELECT has_table_privilege('public', $1, 'SELECT')", table
            )
            assert allowed is False
