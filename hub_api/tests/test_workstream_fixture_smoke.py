"""Smoke test for ingest_sources/workstreams/workstream_usage_hourly reflection.

Proves all three tables are queryable via `install_dal`. Seeds its own
rows rather than relying on any shared seed data, so this test cannot
perturb row counts any other M2b test asserts against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.bundle_install_dal import raw_sql_rows, raw_sql_write


async def test_new_tables_are_queryable_and_start_empty(install_dal: Any) -> None:
    assert "ingest_sources" in install_dal.tables
    assert "workstreams" in install_dal.tables
    assert "workstream_usage_hourly" in install_dal.tables
    for table, sql in (
        ("ingest_sources", "SELECT COUNT(*) AS n FROM ingest_sources"),
        ("workstreams", "SELECT COUNT(*) AS n FROM workstreams"),
        ("workstream_usage_hourly", "SELECT COUNT(*) AS n FROM workstream_usage_hourly"),
    ):
        count = await raw_sql_rows(install_dal, sql)  # noqa: S608 -- fixed table names, no interpolation
        assert count.first()["n"] == 0, f"{table} should start empty"


async def test_a_workstream_row_round_trips(install_dal: Any) -> None:
    source_id = await install_dal.ingest_sources.async_insert(
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="smoke-src-1",
        label="Smoke Source",
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workstream_id = await install_dal.workstreams.async_insert(
        tenant_id=1,
        community_id=None,
        ingest_source_id=source_id,
        platform="twitch",
        source_id="smoke-src-1",
        created_at=datetime.now(UTC),
    )
    row = (await install_dal(install_dal.workstreams.id == workstream_id).select()).first()
    assert row is not None
    assert row.source_id == "smoke-src-1"
    assert row.disabled_at is None


async def test_a_usage_hourly_row_round_trips(install_dal: Any) -> None:
    source_id = await install_dal.ingest_sources.async_insert(
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="smoke-src-2",
        label="Smoke Source 2",
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workstream_id = await install_dal.workstreams.async_insert(
        tenant_id=1,
        community_id=None,
        ingest_source_id=source_id,
        platform="twitch",
        source_id="smoke-src-2",
        created_at=datetime.now(UTC),
    )
    rows = await raw_sql_write(
        install_dal,
        """
        INSERT INTO workstream_usage_hourly
            (tenant_id, community_id, workstream_id, stage, app_id, hour, events,
             invocations, host_calls, actions_delivered, fuel_ms, outbound_bytes,
             media_minutes, recorded_at)
        VALUES
            (:tenant_id, :community_id, :workstream_id, :stage, :app_id, :hour, :events,
             :invocations, :host_calls, :actions_delivered, :fuel_ms, :outbound_bytes,
             :media_minutes, :recorded_at)
        """,
        {
            "tenant_id": 1,
            "community_id": None,
            "workstream_id": str(workstream_id),
            "stage": "ingest",
            "app_id": None,
            "hour": datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC),
            "events": 5,
            "invocations": 0,
            "host_calls": 0,
            "actions_delivered": 0,
            "fuel_ms": 0,
            "outbound_bytes": 1024,
            "media_minutes": None,
            "recorded_at": datetime.now(UTC),
        },
    )
    assert rows.first() is None or len(rows) == 0  # no RETURNING clause
    check = await raw_sql_rows(
        install_dal,
        "SELECT events, workstream_id FROM workstream_usage_hourly WHERE workstream_id = :w",
        {"w": str(workstream_id)},
    )
    row = check.first()
    assert row is not None
    assert row["events"] == 5
    assert row["workstream_id"] == str(workstream_id)
