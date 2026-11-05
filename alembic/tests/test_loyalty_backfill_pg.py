"""Execute the prepared one-way loyalty migration only on disposable real Postgres."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from pg_docker import empty_postgres

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "loyalty_backfill", ROOT / "scripts/db/prepare_loyalty_backfill.py"
)
assert SPEC and SPEC.loader
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)
USER = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(scope="module")
def connection() -> Iterator[Any]:
    """Create a real schema with the same shipped economy SQL as production."""
    with empty_postgres("loyalty-backfill-p4") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE tenants(id INTEGER PRIMARY KEY);
                CREATE TABLE communities(id INTEGER PRIMARY KEY,tenant_id INTEGER REFERENCES tenants(id));
                CREATE TABLE community_members(id SERIAL PRIMARY KEY,community_id INTEGER REFERENCES communities(id),is_active BOOLEAN,left_at TIMESTAMP,removed_at TIMESTAMP);
                INSERT INTO tenants VALUES(1),(2); INSERT INTO communities VALUES(10,1);
            """)
            for name in [
                "bundle_economy_store.sql",
                "bundle_economy_idempotency.sql",
                "bundle_economy_ledger_kinds.sql",
            ]:
                cur.execute((ROOT / "scripts/db" / name).read_text())
            cur.execute(
                "INSERT INTO community_members(community_id,user_uuid,is_active) VALUES(10,%s,TRUE)",
                (USER,),
            )
        try:
            yield conn
        finally:
            conn.close()


@pytest.fixture
def clean_database(connection: Any) -> Any:
    """Reset money tables between test cases without altering membership facts."""
    with connection.cursor() as cur:
        cur.execute("TRUNCATE economy_balances,economy_ledger")
    return connection


def plan(tenant: int = 1, amount: int = 20, cap: int = 1_000_000) -> dict[str, Any]:
    """Prepare an operator-verified mapping before rendering SQL."""
    row = {
        "tenant_id": tenant,
        "community_id": 10,
        "actor_hash": "a" * 64,
        "balance": amount,
    }
    mapping = {
        "tenant_id": tenant,
        "community_id": 10,
        "actor_hash": "a" * 64,
        "user_uuid": USER,
    }
    return backfill.prepare([row], [mapping], cap)


def test_backfill_is_ledgered_and_cannot_apply_twice(clean_database: Any) -> None:
    """The actual migration commits one balance and ledger, then refuses replay."""
    sql = backfill.migration_sql(plan())
    with clean_database.cursor() as cur:
        cur.execute(sql)
        cur.execute("SELECT balance FROM economy_balances")
        assert cur.fetchone()[0] == 20
        cur.execute("SELECT kind,delta,balance_after FROM economy_ledger")
        assert cur.fetchone() == ("admin_adjust", 20, 20)
        with pytest.raises(psycopg2.errors.RaiseException, match="already applied"):
            cur.execute(sql)
        cur.execute("ROLLBACK")
        cur.execute("SELECT SUM(balance) FROM economy_balances")
        assert cur.fetchone()[0] == 20


@pytest.mark.parametrize("case", ["cross_tenant", "existing_balance", "supply_cap"])
def test_backfill_refusals_roll_back_everything(clean_database: Any, case: str) -> None:
    """Tenant mismatch, existing funds and live supply limits cannot mint or overwrite."""
    with clean_database.cursor() as cur:
        if case in {"existing_balance", "supply_cap"}:
            user = (
                USER
                if case == "existing_balance"
                else "22222222-2222-4222-8222-222222222222"
            )
            cur.execute(
                "INSERT INTO economy_balances VALUES(1,10,%s,90,NOW())", (user,)
            )
        sql = backfill.migration_sql(
            plan(tenant=2 if case == "cross_tenant" else 1, cap=100)
        )
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute(sql)
        cur.execute("ROLLBACK")
        cur.execute("SELECT COUNT(*) FROM economy_ledger")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COALESCE(SUM(balance),0) FROM economy_balances")
        assert cur.fetchone()[0] == (0 if case == "cross_tenant" else 90)


def test_revised_plan_cannot_recredit_an_already_migrated_account(
    clean_database: Any,
) -> None:
    """regression: p981-claim-3 -- the replay guard keys on the ACCOUNT, not the plan hash.

    A revised plan (different amount => different plan id) that includes an
    account migrated by an earlier plan must abort even after the member spent
    the balance back to zero, or legacy funds would be credited twice.
    """
    first = backfill.migration_sql(plan(amount=20))
    revised = backfill.migration_sql(plan(amount=30))
    assert first != revised
    with clean_database.cursor() as cur:
        cur.execute(first)
        cur.execute("UPDATE economy_balances SET balance=0")
        with pytest.raises(psycopg2.errors.RaiseException, match="already migrated"):
            cur.execute(revised)
        cur.execute("ROLLBACK")
        cur.execute("SELECT balance FROM economy_balances")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM economy_ledger")
        assert cur.fetchone()[0] == 1
