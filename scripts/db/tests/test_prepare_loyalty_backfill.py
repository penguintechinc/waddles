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


def test_max_supply_cannot_exceed_the_runtime_ceiling() -> None:
    """regression: p981-claim-4 -- a plan cap above the host's supply ceiling is refused."""
    rows, mapping = sample()
    ceiling = module.RUNTIME_MAX_SUPPLY
    with pytest.raises(ValueError, match="runtime supply ceiling"):
        module.prepare(rows, mapping, ceiling + 1)
    plan = module.prepare(rows, mapping, ceiling)
    assert plan["max_supply"] == ceiling
    # An imported (hand-edited / stale) plan is re-validated at SQL render time.
    with pytest.raises(ValueError, match="runtime supply ceiling"):
        module.migration_sql({**plan, "max_supply": ceiling + 1})
    with pytest.raises(ValueError, match="runtime supply ceiling"):
        module.migration_sql({**plan, "max_supply": "1; DROP TABLE economy_ledger"})


@pytest.mark.parametrize(
    "bad_id",
    ["x' OR '1'='1", "%", "a" * 63, "A" * 64, "z" * 64, "0" * 64 + "%", ""],
)
def test_plan_id_is_exact_hex_before_it_reaches_sql(bad_id: str) -> None:
    """regression: p981-claim-F -- plan_id is interpolated into a LIKE; it must be exact lowercase hex."""
    rows, mapping = sample()
    plan = module.prepare(rows, mapping)
    with pytest.raises(ValueError, match="plan_id"):
        module.migration_sql({**plan, "plan_id": bad_id})
    assert plan["plan_id"] in module.migration_sql(plan)


def test_movements_are_revalidated_before_sql() -> None:
    """regression: p981-claim-F -- an imported plan's movement fields never reach SQL unchecked."""
    rows, mapping = sample()
    plan = module.prepare(rows, mapping)
    for patch in (
        {"user_uuid": "11111111-1111-4111-8111-111111111111'); DROP TABLE x;--"},
        {"amount": "5; DROP TABLE x"},
        {"tenant_id": True},
        {"amount": -1},
    ):
        bad = {**plan, "movements": [{**plan["movements"][0], **patch}]}
        with pytest.raises(ValueError):
            module.migration_sql(bad)
