"""Regression test for 0034_upload_abandoned_status (fix/seeder-stalled-upload-recovery).

Same harness convention as `test_0019_kick_app.py` et al. -- no pytest-level fixture runs
Alembic against a real Postgres in CI, so this mocks `alembic.op.execute` and asserts the
exact SQL text `upgrade()`/`downgrade()` emit.

**Revision pinning.** This migration was authored against a chain (0026 seeder -> 0027
lifecycle -> 0028 changelog -> 0029 attribution -> 0030 app schemas -> 0031 signing ->
0032 grants -> 0033 users.uuid) that was still queued/unmerged on other branches at the
time this PR was opened -- `down_revision` below is a forward reference to a revision ID
that may not match 0033's actual final shape by the time this branch merges. Pinning the
exact strings here (rather than e.g. reading them off `alembic_version` at runtime) means
a silent renumbering upstream fails this test loudly instead of forking the migration
chain at merge time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0034_upload_abandoned_status.py"
)


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "migration_0034_upload_abandoned_status", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_down_revision_are_pinned_explicitly() -> None:
    """Fails loudly if this migration is renumbered/rebased without updating this test.

    NOTE: `down_revision` is a forward reference to 0033_hub_users_identity_uuid, which was
    still queued on another branch when this test was written -- verify this still matches
    0033's actual final revision ID at merge time; adjust both here and in the migration
    file if it was renamed.
    """
    module = _load_migration()
    assert module.revision == "0034_upload_abandoned_status"
    assert module.down_revision == "0033_hub_users_identity_uuid"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_adds_abandoned_to_the_status_check_constraint() -> None:
    module = _load_migration()
    with patch.object(module, "op") as mock_op:
        module.upgrade()
    statements = [call.args[0] for call in mock_op.execute.call_args_list]
    check_stmt = next(
        s for s in statements if "app_version_uploads_status_check" in s and "ADD CONSTRAINT" in s
    )
    assert "'ABANDONED'" in check_stmt
    assert "'REJECTED'" in check_stmt  # existing terminal-failure state untouched


def test_upgrade_replaces_the_unique_constraint_with_a_partial_index() -> None:
    module = _load_migration()
    with patch.object(module, "op") as mock_op:
        module.upgrade()
    statements = [call.args[0] for call in mock_op.execute.call_args_list]
    drop_stmt = next(
        s for s in statements if "DROP CONSTRAINT IF EXISTS app_version_uploads_app_id_version_key" in s
    )
    index_stmt = next(
        s for s in statements if "uq_app_version_uploads_active" in s and "CREATE UNIQUE INDEX" in s
    )
    assert drop_stmt  # the old blocks-every-resubmission constraint is removed
    assert "WHERE status NOT IN ('REJECTED', 'ABANDONED')" in index_stmt


def test_downgrade_restores_the_original_unique_constraint_and_check() -> None:
    module = _load_migration()
    with patch.object(module, "op") as mock_op:
        module.downgrade()
    statements = [call.args[0] for call in mock_op.execute.call_args_list]
    assert any("DROP INDEX IF EXISTS uq_app_version_uploads_active" in s for s in statements)
    restore_unique = next(
        s for s in statements if "ADD CONSTRAINT app_version_uploads_app_id_version_key UNIQUE" in s
    )
    assert restore_unique
    restore_check = next(
        s
        for s in statements
        if "app_version_uploads_status_check" in s and "ADD CONSTRAINT" in s
    )
    assert "'ABANDONED'" not in restore_check  # downgrade reverts to the pre-0034 enum
