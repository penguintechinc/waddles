"""Offline backfill safety checks; no production database is contacted."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "prepare_loyalty_backfill",
    Path(__file__).parents[1] / "prepare_loyalty_backfill.py",
)
assert _SPEC and _SPEC.loader
module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(module)


def sample() -> tuple[list[dict], list[dict]]:
    """One legacy pseudonym maps explicitly to one scoped community UUID."""
    return (
        [{"tenant_id": 1, "community_id": 10, "actor_hash": "a" * 64, "balance": 20}],
        [
            {
                "tenant_id": 1,
                "community_id": 10,
                "actor_hash": "a" * 64,
                "user_uuid": "11111111-1111-4111-8111-111111111111",
            }
        ],
    )


def test_plan_is_stable_and_sql_guarded() -> None:
    """Generated SQL is review-only, ledgered, scoped and never drops legacy data."""
    rows, mapping = sample()
    plan = module.prepare(rows, mapping)
    assert plan == module.prepare(rows, mapping)
    assert plan["status"] == "REQUIRES_EXPLICIT_APPROVAL"
    assert plan["total_amount"] == 20
    sql = module.migration_sql(plan)
    assert "LOCK TABLE economy_balances" in sql
    assert "backfill plan already applied" in sql
    assert "community supply ceiling exceeded" in sql
    assert "INSERT INTO economy_ledger" in sql
    assert "DROP TABLE loyalty" not in sql


@pytest.mark.parametrize(
    "case",
    ["unmapped", "duplicate", "negative", "supply", "wrong_scope", "duplicate_mapping"],
)
def test_unsafe_export_cannot_generate_migration(case: str) -> None:
    """Missing identity, corrupt exports and cross-scope mappings fail closed."""
    rows, mapping = sample()
    if case == "unmapped":
        mapping = []
    elif case == "duplicate":
        rows = rows * 2
    elif case == "negative":
        rows[0]["balance"] = -1
    elif case == "supply":
        rows[0]["balance"] = 1_000_001
    elif case == "wrong_scope":
        mapping[0]["tenant_id"] = 2
    elif case == "duplicate_mapping":
        mapping = mapping * 2
    with pytest.raises(ValueError):
        module.prepare(rows, mapping)
