"""Tests for `scripts/db/bundle_reader_role.py` -- the shared reconcile step
`migrations/run-alembic.sh` runs after every `alembic upgrade head`, and that
`0032_bundle_reader_role.py` now also defers its own empty-password refusal to.

# regression: lookup-keep preserved EMPTY reader password; multi-app path off (alpha 2026-10-02)

The empty-password guard is pure Python (raises before touching any DB
connection) and is asserted directly, with no Postgres container needed. The
real-reconcile-against-a-live-role behavior (password actually changes,
grants actually apply) is covered by `test_0032_bundle_reader_role.py`'s
existing real-Postgres `TestIdempotentRoleCreation`/`TestReaderGrants`
suites, which exercise the equivalent SQL this module's
`reconcile_role_and_grants` also runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "db"))
import bundle_reader_role  # noqa: E402


class TestReconcileRefusesEmptyPassword:
    @pytest.mark.parametrize("password", ["", None])
    def test_raises_value_error_on_empty_password(self, password: str | None) -> None:
        with pytest.raises(ValueError, match="DB_READER_PASSWORD"):
            # conn=None is safe here: the guard raises before conn is ever
            # touched -- a real Connection would only be needed past this point.
            bundle_reader_role.reconcile_role_and_grants(None, password)  # type: ignore[arg-type]

    def test_does_not_raise_for_non_empty_password_argument_shape(self) -> None:
        # Confirms the guard itself is the ONLY thing short-circuiting above --
        # a real non-empty password proceeds to use `conn`, which we don't
        # exercise here (that's the real-Postgres coverage noted in the module
        # docstring); this just proves the guard doesn't false-positive.
        class _RaisesOnFirstExecute:
            def execute(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("reached real SQL execution -- guard did not short-circuit")

        with pytest.raises(RuntimeError, match="reached real SQL execution"):
            bundle_reader_role.reconcile_role_and_grants(_RaisesOnFirstExecute(), "real-password")  # type: ignore[arg-type]


class TestRoleAndTableConstants:
    """`scripts/db/bundle_reader_role.TABLES` must stay exactly in sync with
    `0032_bundle_reader_role.py`'s `_READER_TABLES` -- the whole point of this
    shared module is a single list neither call site re-derives independently.
    """

    def test_tables_match_migration_0032(self) -> None:
        import importlib.util

        migration_path = (
            Path(__file__).resolve().parents[1] / "versions" / "0032_bundle_reader_role.py"
        )
        spec = importlib.util.spec_from_file_location("waddles_0032_tables_check", migration_path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        assert bundle_reader_role.TABLES == module._READER_TABLES
        assert bundle_reader_role.ROLE == module._READER_ROLE
        assert bundle_reader_role.PASSWORD_ENV == module._READER_PASSWORD_ENV


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
