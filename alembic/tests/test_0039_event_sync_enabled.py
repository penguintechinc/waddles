"""`0039_event_sync_enabled` -- mocked `op.execute` assertions + drift guard vs `schema.py`.

Same mocked-`alembic.op.execute` methodology as the other `test_00NN_*.py`
siblings in this directory (no DB needed for these; `test_bundle_active_
set_watermark_job.py` + this migration's addition to `pg_docker.py`'s
`_BOOTSTRAP_SQL` cover the real-Postgres execution path). Unlike
`test_0034_schema_drift.py` (which cross-checks against a `flask_core`
SQLAlchemy model), this migration's consumer is `hub_api/services/
schema.py::bind_calendar_sync_tables()`'s pydal `Field` list -- there is
no SQLAlchemy model for `calendar_event_discord_syncs` -- so the drift
guard here cross-checks the migration's `CREATE TABLE` columns against
that pydal binding instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "versions" / "0039_event_sync_enabled.py"
_HUB_API_ROOT = Path(__file__).resolve().parents[2] / "hub_api"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0039_event_sync_enabled", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_sql(method: str) -> str:
    with patch("alembic.op.execute") as mock_execute:
        getattr(_load_migration(), method)()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


def _schema_py_calendar_sync_fields() -> set[str]:
    if str(_HUB_API_ROOT) not in sys.path:
        sys.path.insert(0, str(_HUB_API_ROOT))
    from pydal import DAL
    from services.schema import (
        bind_calendar_sync_tables,  # type: ignore[import-not-found]
    )

    dal = DAL("sqlite:memory")
    bind_calendar_sync_tables(dal, migrate=True)
    return {f for f in dal.calendar_event_discord_syncs.fields if f != "id"}


class TestMigrationMetadata:
    def test_chains_directly_off_0035(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0039_event_sync_enabled"
        assert migration.down_revision == "0035_connection_model_layers"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32


class TestUpgradeSql:
    def test_adds_event_sync_enabled_column(self) -> None:
        sql = _migration_sql("upgrade")
        assert "ADD COLUMN IF NOT EXISTS event_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
        assert "ALTER TABLE guild_tenant_pairings" in sql

    def test_creates_calendar_event_discord_syncs_table(self) -> None:
        sql = _migration_sql("upgrade")
        assert "CREATE TABLE IF NOT EXISTS calendar_event_discord_syncs" in sql
        assert "UNIQUE (event_id, pairing_id)" in sql
        assert "REFERENCES calendar_events(id) ON DELETE CASCADE" in sql
        assert "REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE" in sql

    def test_grants_hub_api_full_privileges(self) -> None:
        sql = _migration_sql("upgrade")
        assert "GRANT DELETE, INSERT, SELECT, UPDATE ON calendar_event_discord_syncs TO hub_api;" in sql
        assert "REVOKE ALL ON calendar_event_discord_syncs FROM PUBLIC;" in sql


class TestDowngradeSql:
    def test_drops_table_and_column(self) -> None:
        sql = _migration_sql("downgrade")
        assert "DROP TABLE IF EXISTS calendar_event_discord_syncs" in sql
        assert "DROP COLUMN IF EXISTS event_sync_enabled" in sql


class TestNoDriftBetweenMigrationAndSchemaPy:
    def test_calendar_event_discord_syncs_columns_match_schema_py(self) -> None:
        sql = _migration_sql("upgrade")
        marker = "CREATE TABLE IF NOT EXISTS calendar_event_discord_syncs ("
        start = sql.find(marker)
        assert start != -1
        paren_start = start + len(marker) - 1
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
        assert end is not None
        body = sql[paren_start + 1 : end]

        sql_columns: set[str] = set()
        depth = 0
        current = []
        parts = []
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
        for part in parts:
            stripped = part.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith(("UNIQUE", "CHECK", "CONSTRAINT", "PRIMARY KEY", "FOREIGN KEY")):
                continue
            name = stripped.split()[0]
            if name == "id":
                continue
            sql_columns.add(name)

        schema_py_columns = _schema_py_calendar_sync_fields()

        missing_from_schema_py = sql_columns - schema_py_columns
        missing_from_sql = schema_py_columns - sql_columns
        assert not missing_from_schema_py, (
            f"migration has column(s) {missing_from_schema_py} with no matching "
            "Field on schema.py::bind_calendar_sync_tables()"
        )
        assert not missing_from_sql, (
            f"schema.py::bind_calendar_sync_tables() declares column(s) {missing_from_sql} "
            "the migration never creates"
        )
