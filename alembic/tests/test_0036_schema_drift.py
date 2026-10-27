"""Schema-drift guard: 0036_role_sync_community_role_bindings's ALTER SQL vs its model.

Unlike `test_0034_schema_drift.py`/`test_0035_schema_drift.py` (which both
cross-check a `CREATE TABLE` body), 0036 only ever `ALTER TABLE`s the
pre-existing `community_role_sync_bindings` table (adds the `community_role`
column + three CHECK constraints + one partial unique index) -- there is no
`CREATE TABLE` block to parse here. This test instead statically asserts the
migration's `ADD COLUMN`/`ADD CONSTRAINT`/`CREATE ... INDEX` statements exist
with the expected shape, and cross-checks the new column name against
`CommunityRoleSyncBinding` (same model `test_0034_schema_drift.py` already
covers for the table's original columns).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0036_role_sync_community_role.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0036_role_sync_community_role", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_sql(direction: str) -> str:
    with patch("alembic.op.execute") as mock_execute:
        getattr(_load_migration(), direction)()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


def _model_columns(class_name: str) -> set[str]:
    import sys

    _FLASK_CORE_ROOT = Path(__file__).resolve().parents[2] / "libs" / "flask_core"
    if str(_FLASK_CORE_ROOT) not in sys.path:
        sys.path.insert(0, str(_FLASK_CORE_ROOT))
    from flask_core.models import guild_pairing  # type: ignore[import-not-found]

    model = getattr(guild_pairing, class_name)
    return {c.name for c in model.__table__.columns}


class TestMigrationMetadata:
    def test_chains_directly_off_0035(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0036_role_sync_community_role"
        assert migration.down_revision == "0035_connection_model_layers"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32


class TestUpgradeAddsCommunityRoleColumn:
    def test_adds_community_role_column(self) -> None:
        sql = _migration_sql("upgrade")
        assert "ADD COLUMN IF NOT EXISTS community_role VARCHAR(20)" in sql

    def test_community_role_is_a_model_column(self) -> None:
        assert "community_role" in _model_columns("CommunityRoleSyncBinding")

    def test_widens_sync_scope_check_to_include_community_role(self) -> None:
        sql = _migration_sql("upgrade")
        assert "sync_scope IN ('subscriber_tier', 'moderator', 'community_role')" in sql

    def test_adds_community_role_value_check(self) -> None:
        sql = _migration_sql("upgrade")
        assert "chk_role_sync_binding_community_role" in sql
        # Isolate the actual CHECK(...) clause's allowed-value list, not the whole SQL
        # blob (which also carries a `COMMENT ON COLUMN` mentioning "community-owner"
        # in prose, explaining why it's excluded -- that's not the CHECK itself).
        marker = "chk_role_sync_binding_community_role CHECK ("
        start = sql.find(marker)
        assert start != -1
        end = sql.find(")", start)
        allowed_values_clause = sql[start : end + 1]
        assert "'community-admin'" in allowed_values_clause
        assert "'moderator'" in allowed_values_clause
        assert "'vip'" in allowed_values_clause
        assert "'member'" in allowed_values_clause
        # community-owner must never be an assignable sync target (owner-protection).
        assert "community-owner" not in allowed_values_clause

    def test_rewrites_scope_tier_check_as_three_way(self) -> None:
        sql = _migration_sql("upgrade")
        assert "chk_role_sync_binding_scope_tier" in sql
        assert "sync_scope = 'community_role'" in sql
        assert "community_role IS NOT NULL" in sql

    def test_adds_community_role_partial_unique_index(self) -> None:
        sql = _migration_sql("upgrade")
        assert "uq_role_sync_binding_community_role" in sql
        assert "WHERE sync_scope = 'community_role'" in sql


class TestDowngradeReversesEveryChange:
    def test_drops_community_role_column(self) -> None:
        sql = _migration_sql("downgrade")
        assert "DROP COLUMN IF EXISTS community_role" in sql

    def test_restores_two_way_sync_scope_check(self) -> None:
        sql = _migration_sql("downgrade")
        assert "sync_scope IN ('subscriber_tier', 'moderator')" in sql

    def test_drops_community_role_index_and_constraints(self) -> None:
        sql = _migration_sql("downgrade")
        assert "DROP INDEX IF EXISTS uq_role_sync_binding_community_role" in sql
        assert "DROP CONSTRAINT IF EXISTS chk_role_sync_binding_community_role" in sql
