"""services/dispatch_log.py -- action_dispatch_log pydal binding + write helper."""

from __future__ import annotations

from datetime import datetime

import pytest
from penguin_dal import AsyncDB
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table

from services.dispatch_log import record_dispatch


def _create_dispatch_log_tables(conn):
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True, autoincrement=True))
    Table(
        "communities",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer),
    )
    Table(
        "action_dispatch_log",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("app_id", String(255), nullable=False),
        Column("target_type", String(255), nullable=False),
        Column("status", String(255), nullable=False),
        Column("attempt", Integer, default=1),
        Column("http_status", Integer),
        Column("detail", String(500), default=""),
        Column("envelope_ts", DateTime),
        Column("dispatched_at", DateTime),
    )
    metadata.create_all(conn)


@pytest.fixture
async def dal() -> AsyncDB:
    """A real in-memory penguin_dal.AsyncDB with tenants/communities/action_dispatch_log tables."""
    db = AsyncDB("sqlite://", pool_size=1)
    async with db.engine.begin() as conn:
        await conn.run_sync(_create_dispatch_log_tables)
    await db.reflect()
    yield db
    await db.close()


async def test_record_dispatch_inserts_a_row(dal: AsyncDB) -> None:
    await dal.tenants.async_insert()
    await dal.communities.async_insert(tenant_id=1)

    await record_dispatch(
        dal,
        tenant_id=1,
        community_id=1,
        app_id="waddles.bot.shoutout.default",
        target_type="webhook",
        status="success",
        attempt=1,
        http_status=200,
        detail="delivered, HTTP 200",
        envelope_ts=datetime(2026, 8, 31, 12, 0, 0),
    )

    rows = await dal(dal.action_dispatch_log.id > 0).select()
    assert len(rows) == 1
    assert rows.first().status == "success"
    assert rows.first().target_type == "webhook"
    assert rows.first().http_status == 200


async def test_record_dispatch_truncates_long_detail(dal: AsyncDB) -> None:
    await dal.tenants.async_insert()

    long_detail = "x" * 1000
    await record_dispatch(
        dal,
        tenant_id=1,
        community_id=None,
        app_id="waddles.bot.shoutout.default",
        target_type="webhook",
        status="non_retryable_failure",
        attempt=1,
        http_status=None,
        detail=long_detail,
        envelope_ts=None,
    )

    rows = await dal(dal.action_dispatch_log.id > 0).select()
    assert len(rows.first().detail) == 500
