"""Tests for 0072_reputation_host_ops: chain position, id length, single head, shared DDL.

The Postgres behaviour of the DDL (clamp CHECK, idempotency index) is exercised against a
real database by `core/bundle_host_reputation/tests/postgres_integration.rs`, which loads the
same `scripts/db/bundle_reputation_host_ops.sql` this migration executes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0072_reputation_host_ops.py"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0072", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chains_off_0071_and_revision_id_fits_varchar32() -> None:
    m = _load()
    assert m.down_revision == "0071_economy_ledger_kinds"
    assert len(m.revision) <= 32


def test_the_alembic_history_has_exactly_one_head() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0072_reputation_host_ops"], heads


def test_the_shared_ddl_file_exists_and_declares_the_clamp_and_idempotency_objects() -> None:
    sql = (_ROOT / "scripts" / "db" / "bundle_reputation_host_ops.sql").read_text(encoding="utf-8")
    assert sql.strip(), "the DDL file must not be empty"
    for needle in (
        "ck_bundle_reputation_scores_balance_range",
        "uq_bundle_reputation_adjustments_idem",
        "ck_bundle_reputation_adjustments_kind",
    ):
        assert needle in sql, needle
