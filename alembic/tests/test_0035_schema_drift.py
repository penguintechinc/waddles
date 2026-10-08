"""Schema-drift guard: 0035_connection_model_layers's new-table SQL vs its SQLAlchemy models.

Same methodology as `test_0034_schema_drift.py`: statically extracts
every `CREATE TABLE` column this migration's `upgrade()` emits (mocked
`alembic.op.execute`, no DB needed) and cross-checks it against the
matching `libs/flask_core/flask_core/models/guild_pairing.py` model's own
declared columns. Only covers the two genuinely new tables
(`platform_connections`, `community_connection_access`) -- the renamed
`tenant_platform_apps` table isn't created via `CREATE TABLE` in this
migration (it's an `ALTER TABLE ... RENAME`), so its column parity with
`TenantPlatformApp` is instead proven dynamically against a real Postgres
in `test_0035_connection_model_layers.py`
(`TestTenantPlatformAppsRename.test_renamed_table_has_expected_columns`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0035_connection_model_layers.py"
)

# (CREATE TABLE name, model class name) pairs this migration/models module share.
_TABLE_MODEL_PAIRS = [
    ("platform_connections", "PlatformConnection"),
    ("community_connection_access", "CommunityConnectionAccess"),
]


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0035_connection_model_layers", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_sql() -> str:
    with patch("alembic.op.execute") as mock_execute:
        _load_migration().upgrade()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


def _split_top_level(body: str) -> list[str]:
    """Split a `CREATE TABLE (...)` body on top-level commas (depth-tracked, ignores commas inside parens)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _sql_columns_for_table(sql: str, table: str) -> set[str]:
    """Extract column names from this migration's `CREATE TABLE IF NOT EXISTS <table> (...)` block."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = sql.find(marker)
    assert start != -1, f"CREATE TABLE IF NOT EXISTS {table} not found in migration SQL"
    paren_start = start + len(marker) - 1  # index of the opening '('
    depth = 0
    end = None
    for idx in range(paren_start, len(sql)):
        if sql[idx] == "(":
            depth += 1
        elif sql[idx] == ")":
            depth -= 1
            if depth == 0:
                end = idx
                break
    assert end is not None, f"unbalanced parens in CREATE TABLE for {table}"
    body = sql[paren_start + 1 : end]
    columns: set[str] = set()
    for part in _split_top_level(body):
        stripped = part.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith(("UNIQUE", "CHECK", "CONSTRAINT", "PRIMARY KEY", "FOREIGN KEY")):
            continue
        name = stripped.split()[0]
        columns.add(name)
    return columns


def _model_columns(class_name: str) -> set[str]:
    import sys

    _FLASK_CORE_ROOT = Path(__file__).resolve().parents[2] / "libs" / "flask_core"
    if str(_FLASK_CORE_ROOT) not in sys.path:
        sys.path.insert(0, str(_FLASK_CORE_ROOT))
    from flask_core.models import guild_pairing  # type: ignore[import-not-found]

    model = getattr(guild_pairing, class_name)
    return {c.name for c in model.__table__.columns}


class TestMigrationMetadata:
    def test_chains_directly_off_0034(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0035_connection_model_layers"
        assert migration.down_revision == "0034_bar_citizen_guild_pairing"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32


class TestNoDriftBetweenMigrationAndModels:
    @pytest.mark.parametrize("table,model_name", _TABLE_MODEL_PAIRS)
    def test_migration_columns_match_model_columns(self, table: str, model_name: str) -> None:
        sql = _migration_sql()
        sql_columns = _sql_columns_for_table(sql, table)
        model_columns = _model_columns(model_name)

        missing_from_model = sql_columns - model_columns
        missing_from_sql = model_columns - sql_columns

        assert not missing_from_model, (
            f"{table}: migration has column(s) {missing_from_model} with no matching "
            f"field on model {model_name}"
        )
        assert not missing_from_sql, (
            f"{table}: model {model_name} declares column(s) {missing_from_sql} the "
            "migration never creates"
        )


class TestDowngradeDropsNewTables:
    def test_downgrade_drops_both_new_tables(self) -> None:
        with patch("alembic.op.execute") as mock_execute:
            _load_migration().downgrade()
        sql = "\n".join(call.args[0] for call in mock_execute.call_args_list)
        for table, _ in _TABLE_MODEL_PAIRS:
            assert f"DROP TABLE IF EXISTS {table}" in sql, f"downgrade() missing DROP for {table}"

    def test_downgrade_restores_original_table_name_and_trigger(self) -> None:
        with patch("alembic.op.execute") as mock_execute:
            _load_migration().downgrade()
        sql = "\n".join(call.args[0] for call in mock_execute.call_args_list)
        assert "RENAME TO tenant_platform_credentials" in sql
        assert "trg_reject_global_tenant_credentials" in sql
        assert "DROP VIEW IF EXISTS tenant_platform_credentials" in sql
