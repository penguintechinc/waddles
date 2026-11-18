"""Community tier allocation on REAL Postgres: concurrent allocators race for the last slot."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import community_tier_service as svc  # noqa: E402
from services.schema import bind_community_tier_tables  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

RACERS = 8


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    with migrated_postgres(f"cts-{os.getpid()}") as db:
        yield db


def _seed(pg: PgTestDatabase, slug: str, n: int) -> tuple[int, list[int]]:
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
        tenant_id = int(cur.fetchone()[0])
        ids = []
        for i in range(n):
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
                (f"{slug}-{i}", tenant_id),
            )
            ids.append(int(cur.fetchone()[0]))
        return tenant_id, ids
    finally:
        conn.close()


@pytest.fixture
def adal(pg: PgTestDatabase) -> Iterator[Any]:
    url = pg.dsn.replace("postgresql://", "postgres://", 1)
    async_dal = AsyncDAL(url, pool_size=RACERS)
    bind_community_tier_tables(async_dal.dal)
    yield async_dal
    async_dal.dal.close()


async def test_real_postgres_race_for_last_slot_one_winner(pg: PgTestDatabase, adal: Any) -> None:
    tenant_id, cids = _seed(pg, "race", RACERS)
    store = svc.PoolStore(adal, adal.dal)
    await store.put(
        tenant_id, svc.TIER_CODE_PREMIUM, 1, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc()
    )
    pool = svc.StorePoolProvider(store)
    results = await asyncio.gather(
        *[
            svc.allocate(
                adal, adal.dal, tenant_id=tenant_id, community_id=c,
                tier=svc.TIER_CODE_PREMIUM, actor=None,
                source=svc.SOURCE_TENANT_ADMIN, pool=pool,
            )
            for c in cids
        ],
        return_exceptions=True,
    )  # fmt: skip
    wins = [r for r in results if isinstance(r, svc.AllocationResult)]
    losses = [r for r in results if isinstance(r, svc.PoolExhausted)]
    assert len(wins) == 1 and len(losses) == RACERS - 1, results

    conn = psycopg2.connect(pg.dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM community_tier_allocations "
            "WHERE tenant_id = %s AND released_at IS NULL",
            (tenant_id,),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM communities WHERE tenant_id = %s AND is_premium IS TRUE "
            "AND license_tier = 'premium'",
            (tenant_id,),
        )
        assert cur.fetchone()[0] == 1  # denormalized copy written in the same txn
    finally:
        conn.close()


async def test_real_postgres_within_pool_then_blocked_and_usage_single_tier(
    pg: PgTestDatabase, adal: Any
) -> None:
    tenant_id, cids = _seed(pg, "chg", 3)
    store = svc.PoolStore(adal, adal.dal)
    await store.put(
        tenant_id, svc.TIER_CODE_PREMIUM, 2, svc.POOL_SOURCE_LICENSE_SERVER, svc.now_utc()
    )
    pool = svc.StorePoolProvider(store)

    async def alloc(c: int, tier: int) -> svc.AllocationResult:
        return await svc.allocate(
            adal, adal.dal, tenant_id=tenant_id, community_id=c, tier=tier, actor=None,
            source=svc.SOURCE_TENANT_ADMIN, pool=pool,
        )  # fmt: skip

    await alloc(cids[0], svc.TIER_CODE_PREMIUM)
    await alloc(cids[1], svc.TIER_CODE_PREMIUM)
    with pytest.raises(svc.PoolExhausted) as exc:
        await alloc(cids[2], svc.TIER_CODE_PREMIUM)
    assert exc.value.required_tier == svc.TIER_CODE_PREMIUM
    usage = await svc.pool_usage(adal, adal.dal, tenant_id, pool=pool)
    assert list(usage) == [svc.TIER_CODE_PREMIUM]
    assert usage[svc.TIER_CODE_PREMIUM].used == 2
    await alloc(cids[0], svc.TIER_CODE_FREE)  # release frees the slot
    await alloc(cids[2], svc.TIER_CODE_PREMIUM)


class _StalePrefetch:
    """Provider whose answer predates a pool shrink (models the pre-lock async lookup)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    async def get_pool(self, tenant_id: int, tier_code: int) -> svc.PoolInfo:
        return svc.PoolInfo(self.limit, svc.now_utc(), False, "operator_set")


async def test_real_postgres_pool_shrink_after_prefetch_blocks_allocation(
    pg: PgTestDatabase, adal: Any
) -> None:
    tenant_id, cids = _seed(pg, "shrink", RACERS)
    store = svc.PoolStore(adal, adal.dal)
    await store.put(
        tenant_id, svc.TIER_CODE_PREMIUM, 1, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc()
    )
    results = await asyncio.gather(
        *[
            svc.allocate(
                adal, adal.dal, tenant_id=tenant_id, community_id=c,
                tier=svc.TIER_CODE_PREMIUM, actor=None,
                source=svc.SOURCE_TENANT_ADMIN, pool=_StalePrefetch(100),
            )
            for c in cids
        ],
        return_exceptions=True,
    )  # fmt: skip
    wins = [r for r in results if isinstance(r, svc.AllocationResult)]
    assert len(wins) == 1, results  # stale prefetch of 100 never lets more than the real 1 in


async def test_real_postgres_concurrent_pool_upserts_leave_one_row(
    pg: PgTestDatabase, adal: Any
) -> None:
    tenant_id, _ = _seed(pg, "upsert", 0)
    store = svc.PoolStore(adal, adal.dal)
    await asyncio.gather(
        *[
            store.put(
                tenant_id, svc.TIER_CODE_PREMIUM, n, svc.POOL_SOURCE_LICENSE_SERVER,
                svc.now_utc(),
            )
            for n in range(RACERS)
        ]
    )  # fmt: skip
    conn = psycopg2.connect(pg.dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM tenant_license_pools WHERE tenant_id = %s", (tenant_id,))
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


async def test_real_postgres_read_paths_never_idle_in_transaction(
    pg: PgTestDatabase, adal: Any
) -> None:
    tenant_id, cids = _seed(pg, "idle", 1)
    store = svc.PoolStore(adal, adal.dal)
    await store.put(
        tenant_id, svc.TIER_CODE_PREMIUM, 3, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc()
    )
    pool = svc.StorePoolProvider(store)
    await store.get(tenant_id, svc.TIER_CODE_PREMIUM)
    await svc.pool_usage(adal, adal.dal, tenant_id, pool=pool)
    await svc.touch_active(adal, adal.dal, cids[0])
    await svc.resolve_effective_tier(adal, adal.dal, tenant_id, cids[0])
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT query FROM pg_stat_activity WHERE state = 'idle in transaction' "
            "AND pid <> pg_backend_pid()"
        )
        # pydal's own `SELECT 1;` connection-liveness ping is not ours; anything else would be
        # one of this service's jobs leaving its read transaction open.
        assert [q for (q,) in cur.fetchall() if q.strip() != "SELECT 1;"] == []
    finally:
        conn.close()


async def test_real_postgres_stale_rows_keep_slots_under_race(
    pg: PgTestDatabase, adal: Any
) -> None:
    """Regression: 31-day-old rows still count; racers for a 'freed' slot all lose."""
    tenant_id, cids = _seed(pg, "stale", RACERS)
    store = svc.PoolStore(adal, adal.dal)
    pro = svc.TIER_CODE_PREMIUM
    await store.put(tenant_id, pro, 2, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())
    pool = svc.StorePoolProvider(store)

    async def _alloc(c: int) -> svc.AllocationResult:
        return await svc.allocate(
            adal, adal.dal, tenant_id=tenant_id, community_id=c, tier=pro, actor=None,
            source=svc.SOURCE_TENANT_ADMIN, pool=pool,
        )  # fmt: skip

    await _alloc(cids[0])
    await _alloc(cids[1])
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    try:
        conn.cursor().execute(
            "UPDATE community_tier_allocations SET last_active_at = now() - interval '31 days' "
            "WHERE tenant_id = %s",
            (tenant_id,),
        )
        results = await asyncio.gather(*[_alloc(c) for c in cids[2:]], return_exceptions=True)
        assert all(isinstance(r, svc.PoolExhausted) for r in results), results
        usage = (await svc.pool_usage(adal, adal.dal, tenant_id, pool=pool))[pro]
        assert (usage.used, usage.available, usage.stale_excluded) == (2, 0, 2)
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM community_tier_allocations "
            "WHERE tenant_id = %s AND released_at IS NULL",
            (tenant_id,),
        )
        assert cur.fetchone()[0] == 2
    finally:
        conn.close()
