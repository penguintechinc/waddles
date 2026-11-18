"""Tenant Do-Not-Sell writer on REAL Postgres: atomic ON CONFLICT insert, concurrent double-apply.

Skipped (never failed) without the docker CLI.
"""

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

from services import admin_data_privacy_service as svc  # noqa: E402
from services.cookie_consent_service import has_tenant_do_not_sell  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    with migrated_postgres(f"admin-dns-{os.getpid()}") as db:
        yield db


@pytest.fixture
def ids(pg: PgTestDatabase) -> dict[str, int]:
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        out: dict[str, int] = {}
        for name in ("a", "b"):
            cur.execute(
                "INSERT INTO tenants (slug, is_active) VALUES (%s, TRUE) RETURNING id",
                (f"dns-{name}-{os.getpid()}-{id(out)}",),
            )
            out[f"t{name}"] = int(cur.fetchone()[0])
        for name in ("u1", "u2", "u3"):
            cur.execute("INSERT INTO hub_users DEFAULT VALUES RETURNING id")
            out[name] = int(cur.fetchone()[0])
        return out
    finally:
        conn.close()


@pytest.fixture
def adal(pg: PgTestDatabase) -> Iterator[Any]:
    async_dal = AsyncDAL(pg.dsn.replace("postgresql://", "postgres://", 1), pool_size=6)
    yield async_dal
    async_dal.dal.close()


async def test_concurrent_double_apply_is_atomic_on_postgres(
    adal: Any, ids: dict[str, int]
) -> None:
    outcomes = await asyncio.gather(
        *[
            svc.apply_do_not_sell(adal, adal.dal, tenant_id=ids["ta"], user_id=ids["u1"])
            for _ in range(12)
        ]
    )
    assert sorted(outcomes) == [False] * 11 + [True]
    t = adal.dal.tenant_do_not_sell
    assert adal.dal((t.tenant_id == ids["ta"]) & (t.user_id == ids["u1"])).count() == 1
    adal.dal.commit()


async def test_bulk_style_loop_survives_duplicates_and_stays_tenant_scoped(
    adal: Any, ids: dict[str, int]
) -> None:
    results = []
    for user in (ids["u2"], ids["u2"], ids["u3"]):  # a duplicate mid-loop must not poison the rest
        results.append(
            await svc.apply_do_not_sell(adal, adal.dal, tenant_id=ids["ta"], user_id=user)
        )
    assert results == [True, False, True]
    assert await has_tenant_do_not_sell(adal, adal.dal, tenant_id=ids["ta"], user_id=ids["u2"])
    assert not await has_tenant_do_not_sell(adal, adal.dal, tenant_id=ids["tb"], user_id=ids["u2"])
