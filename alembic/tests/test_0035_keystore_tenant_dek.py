"""Regression test for 0035_keystore_tenant_dek.

Same harness convention as `test_0019_kick_app.py`/`test_0018_slack_youtube_
apps.py` -- this repo has no pytest-level fixture that runs Alembic against
a real Postgres in CI, so these tests mock `alembic.op.execute` and assert
the exact SQL text this migration's `upgrade()`/`downgrade()` emit.

**Revision pinning note:** this migration was authored against a worktree
whose visible `alembic/versions/` head was `0026_app_install_approval_
source`, but the real merge-queue chain runs through
`0034_upload_abandoned_status` (not present in this checkout). `revision`/
`down_revision` are pinned to `0035_keystore_tenant_dek` /
`0034_upload_abandoned_status` per that queue -- **the single-head check
below only verifies uniqueness among files actually present in this
checkout; it cannot detect a real collision against `0034_upload_
abandoned_status` until that file exists here too.** Re-verify (and
renumber/re-chain if needed) once both files are visible in the same
checkout, e.g. at merge time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0035_keystore_tenant_dek.py"
)

EXPECTED_REVISION = "0035_keystore_tenant_dek"
EXPECTED_DOWN_REVISION = "0034_upload_abandoned_status"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0035_keystore", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


@pytest.fixture
def upgrade_sql(migration) -> str:
    with patch("alembic.op.execute") as mock_execute:
        migration.upgrade()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


@pytest.fixture
def downgrade_sql(migration) -> str:
    with patch("alembic.op.execute") as mock_execute:
        migration.downgrade()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


class TestMigrationMetadata:
    def test_pinned_revision_and_down_revision(self, migration) -> None:
        assert migration.revision == EXPECTED_REVISION
        assert migration.down_revision == EXPECTED_DOWN_REVISION

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration) -> None:
        # alembic_version.version_num is VARCHAR(32) -- a too-long revision
        # id fails silently truncated or raises at stamp time.
        assert len(migration.revision) <= 32
        assert len(migration.down_revision) <= 32

    def test_single_head_among_files_present_in_this_checkout(self) -> None:
        """No other version file *actually present here* also chains off
        `0034_upload_abandoned_status` -- see module docstring: this
        cannot see a real collision until that file exists in the same
        checkout, so it only guards against a second *local* collision.
        """
        versions_dir = Path(__file__).resolve().parent.parent / "versions"
        down_revisions = []
        for path in versions_dir.glob("*.py"):
            if path.name == "__init__.py":
                continue
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            down_revisions.append(module.down_revision)

        assert down_revisions.count(EXPECTED_DOWN_REVISION) == 1, (
            f"more than one migration in this checkout chains off "
            f"{EXPECTED_DOWN_REVISION!r} -- alembic would report multiple heads"
        )


class TestUpgradeCreatesKeystoreSchema:
    def test_creates_keystore_schema(self, upgrade_sql) -> None:
        assert "CREATE SCHEMA IF NOT EXISTS keystore" in upgrade_sql

    def test_creates_tenant_encryption_keys_table(self, upgrade_sql) -> None:
        assert "CREATE TABLE IF NOT EXISTS keystore.tenant_encryption_keys" in upgrade_sql
        assert "wrapped_dek   BYTEA" in upgrade_sql
        assert "usage_count   BIGINT NOT NULL DEFAULT 0" in upgrade_sql
        assert "UNIQUE (tenant_id, dek_version)" in upgrade_sql

    def test_kek_kind_and_status_check_constraints(self, upgrade_sql) -> None:
        assert "CHECK (kek_kind IN ('platform', 'customer_kms'))" in upgrade_sql
        assert "CHECK (status IN ('active', 'retired', 'destroyed'))" in upgrade_sql

    def test_wrapped_dek_present_unless_destroyed_check_constraint(self, upgrade_sql) -> None:
        assert "ck_tenant_encryption_keys_wrapped_dek_present" in upgrade_sql
        assert "CHECK (status = 'destroyed' OR wrapped_dek IS NOT NULL)" in upgrade_sql

    def test_creates_key_tombstones_table(self, upgrade_sql) -> None:
        assert "CREATE TABLE IF NOT EXISTS keystore.key_tombstones" in upgrade_sql
        assert "shredded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()" in upgrade_sql

    def test_every_ddl_statement_is_idempotent(self, upgrade_sql) -> None:
        # IF NOT EXISTS on every CREATE, so a second `alembic upgrade` (or a
        # partially-applied prior run) never errors.
        assert "CREATE TABLE keystore." not in upgrade_sql
        assert upgrade_sql.count("CREATE TABLE IF NOT EXISTS keystore.") == 2


class TestDowngradeRemovesEverythingUpgradeAdds:
    def test_drops_key_tombstones_before_tenant_encryption_keys(self, downgrade_sql) -> None:
        # No FK between them, but tombstones are the durable shred record --
        # dropping it first mirrors "least-durable-artifact-first" ordering.
        assert downgrade_sql.index("DROP TABLE IF EXISTS keystore.key_tombstones") < downgrade_sql.index(
            "DROP TABLE IF EXISTS keystore.tenant_encryption_keys"
        )

    def test_drops_schema_last(self, downgrade_sql) -> None:
        assert downgrade_sql.rstrip().endswith("DROP SCHEMA IF EXISTS keystore")

    def test_downgrade_is_idempotent(self, migration) -> None:
        with patch("alembic.op.execute") as mock_execute:
            migration.downgrade()
        first_run = [call.args[0] for call in mock_execute.call_args_list]

        with patch("alembic.op.execute") as mock_execute:
            migration.downgrade()
        second_run = [call.args[0] for call in mock_execute.call_args_list]

        assert first_run == second_run
