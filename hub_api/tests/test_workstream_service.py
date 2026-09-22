"""Tests for the standalone workstream creation/disable helpers (spec Sec5.11, Sec6.11, D30).

`create_workstream_for_source()` is exercised standalone here -- it is
NOT called by `create_source()` (`ingest_source_service.py` routes that
through its own `engine.begin()` block instead, Decision #18(a)); this
function remains available, idempotent, and tested for any other caller
that needs to ensure a source's workstream exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.workstream_service import (
    create_workstream_for_source,
    disable_workstream_for_source,
    get_workstream_for_source,
)


async def _seed_source(install_dal: Any, source_id: str = "wsvc-1") -> int:
    now = datetime.now(UTC)
    return await install_dal.ingest_sources.async_insert(
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id=source_id,
        label="X",
        enabled=True,
        created_at=now,
        updated_at=now,
    )


async def test_create_workstream_for_source_creates_exactly_one_row(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    row = await create_workstream_for_source(
        install_dal,
        ingest_source_id=source_id,
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="wsvc-1",
    )
    assert row.platform == "twitch"
    assert row.disabled_at is None
    assert await install_dal(install_dal.workstreams.id > 0).count() == 1


async def test_create_workstream_for_source_is_idempotent(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    first = await create_workstream_for_source(
        install_dal,
        ingest_source_id=source_id,
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="wsvc-1",
    )
    second = await create_workstream_for_source(
        install_dal,
        ingest_source_id=source_id,
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="wsvc-1",
    )
    assert first.id == second.id
    assert await install_dal(install_dal.workstreams.id > 0).count() == 1


async def test_disable_workstream_for_source_sets_disabled_at(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    await create_workstream_for_source(
        install_dal,
        ingest_source_id=source_id,
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="wsvc-1",
    )
    await disable_workstream_for_source(install_dal, ingest_source_id=source_id)
    row = await get_workstream_for_source(install_dal, ingest_source_id=source_id)
    assert row is not None
    assert row.disabled_at is not None


async def test_disable_workstream_for_source_is_a_noop_when_none_exists(install_dal: Any) -> None:
    await disable_workstream_for_source(install_dal, ingest_source_id=999999)


async def test_disable_workstream_for_source_is_idempotent(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    await create_workstream_for_source(
        install_dal,
        ingest_source_id=source_id,
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="wsvc-1",
    )
    await disable_workstream_for_source(install_dal, ingest_source_id=source_id)
    await disable_workstream_for_source(install_dal, ingest_source_id=source_id)
    row = await get_workstream_for_source(install_dal, ingest_source_id=source_id)
    assert row.disabled_at is not None


async def test_get_workstream_for_source_returns_none_when_absent(install_dal: Any) -> None:
    assert await get_workstream_for_source(install_dal, ingest_source_id=999999) is None
