"""Token-debit idempotency on a REAL Postgres: concurrent same-key debits charge exactly once.

sqlite cannot show the cross-connection race: pooled connections can all pass the replay
pre-check and decrement, and only the UNIQUE `idempotency_key` index (alembic 0135) stops the
losers' ledger INSERT -- whose rollback must also undo the loser's decrement. The test applies
0135's own shipped SQL over the 076 ledger tables. Skipped (never failed) without docker.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"
if str(_ALEMBIC_DIR / "tests") not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_DIR / "tests"))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.schema import bind_token_billing_tables  # noqa: E402
from services.token_billing_service import debit_tokens  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_DDL = """
CREATE TABLE IF NOT EXISTS token_products (
    id SERIAL PRIMARY KEY, key VARCHAR(100) NOT NULL UNIQUE, name VARCHAR(255) NOT NULL,
    unit VARCHAR(50) NOT NULL DEFAULT 'token', price_cents INTEGER NOT NULL,
    tokens_granted INTEGER NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS community_token_balances (
    id SERIAL PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id),
    product_id INTEGER NOT NULL REFERENCES token_products(id),
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE (community_id, product_id));
CREATE TABLE IF NOT EXISTS token_transactions (
    id SERIAL PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id),
    product_id INTEGER NOT NULL REFERENCES token_products(id), delta INTEGER NOT NULL,
    reason VARCHAR(255) NOT NULL, ref VARCHAR(255),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    created_at TIMESTAMPTZ DEFAULT NOW());
"""


def _migration_sql() -> tuple[str, ...]:
    """Load alembic 0135's own statements so the test exercises the shipped DDL."""
    path = _ALEMBIC_DIR / "versions" / "0135_token_debit_idempotency.py"
    spec = importlib.util.spec_from_file_location("mig_0135", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.UPGRADE_SQL)


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    """Migrated Postgres with the 076 ledger tables plus 0135's idempotency column/index."""
    with migrated_postgres(f"token-idem-{os.getpid()}") as db:
        conn = psycopg2.connect(db.dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(_DDL)
                for stmt in _migration_sql():
                    cur.execute(stmt)
        finally:
            conn.close()
        yield db


@pytest.fixture
def adal(pg: PgTestDatabase) -> Iterator[AsyncDAL]:
    """A pooled AsyncDAL (4 connections) so debits really run on separate connections."""
    async_dal = AsyncDAL(pg.dsn.replace("postgresql://", "postgres://", 1), pool_size=4)
    bind_token_billing_tables(async_dal.dal, migrate=False)
    try:
        yield async_dal
    finally:
        try:
            async_dal.dal.close()
        finally:
            async_dal.executor.shutdown(wait=True)


def _seed(pg: PgTestDatabase, balance: int) -> int:
    """Insert a community + product + balance row; return the community id."""
    conn = psycopg2.connect(pg.dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_products (key, name, price_cents, tokens_granted) "
                "VALUES ('transcoding_minutes', 'T', 1, 1) ON CONFLICT (key) DO NOTHING"
            )
            cur.execute("SELECT id FROM token_products WHERE key = 'transcoding_minutes'")
            product_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES (%s, 0) RETURNING id",
                (f"c-{os.urandom(4).hex()}",),
            )
            community_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO community_token_balances (community_id, product_id, balance) "
                "VALUES (%s, %s, %s)",
                (community_id, product_id, balance),
            )
        return community_id
    finally:
        conn.close()


def _balance_and_rows(pg: PgTestDatabase, community_id: int) -> tuple[int, int]:
    """Return (balance, ledger row count) read over a fresh connection."""
    conn = psycopg2.connect(pg.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM community_token_balances WHERE community_id = %s",
                (community_id,),
            )
            balance = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM token_transactions WHERE community_id = %s",
                (community_id,),
            )
            return balance, int(cur.fetchone()[0])
    finally:
        conn.close()


async def _debit(
    adal: AsyncDAL, community_id: int, key: str | None, actor: int | None = None
) -> Any:
    return await debit_tokens(
        adal,
        adal.dal,
        community_id=community_id,
        product_key="transcoding_minutes",
        amount=3,
        reason="t",
        idempotency_key=key,
        actor_user_id=actor,
    )


async def test_concurrent_same_key_debits_once(pg: PgTestDatabase, adal: AsyncDAL) -> None:
    """12 concurrent same-key debits: one charge, one ledger row, one transaction id."""
    cid = _seed(pg, balance=30)
    results = await asyncio.gather(*[_debit(adal, cid, "stream:9:adm-1") for _ in range(12)])
    assert all(r.ok for r in results)
    assert len({r.transaction_id for r in results}) == 1
    assert _balance_and_rows(pg, cid) == (27, 1)


async def test_replay_then_new_key(pg: PgTestDatabase, adal: AsyncDAL) -> None:
    """A replay returns the original; a different key debits again."""
    cid = _seed(pg, balance=30)
    first = await _debit(adal, cid, "k1")
    replay = await _debit(adal, cid, "k1")
    other = await _debit(adal, cid, "k2")
    assert replay.transaction_id == first.transaction_id
    assert other.transaction_id != first.transaction_id
    assert _balance_and_rows(pg, cid) == (24, 2)


async def test_racing_actors_on_one_key_charge_once(pg: PgTestDatabase, adal: AsyncDAL) -> None:
    """Racing actors on one key: exactly one debit; every other actor is refused (409 outcome)."""
    cid = _seed(pg, balance=30)
    results = await asyncio.gather(*[_debit(adal, cid, "shared", actor=n) for n in range(1, 7)])
    winners = [r for r in results if r.ok]
    refused = [r for r in results if not r.ok]
    assert len(winners) >= 1
    assert all(r.blocked_reason == "idempotency_key_conflict" for r in refused)
    assert _balance_and_rows(pg, cid) == (27, 1)
