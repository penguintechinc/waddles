"""Tests for the per-community usage query.

Filters, aggregation-at-query-time, pagination (D31).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.errors import ApiError
from services.usage_query_service import query_usage


async def _seed_row(install_dal: Any, **overrides: Any) -> None:
    base = {
        "tenant_id": 1,
        "community_id": None,
        "workstream_id": "ws-1",
        "stage": "ingest",
        "app_id": None,
        "hour": datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
        "events": 1,
        "invocations": 0,
        "host_calls": 0,
        "actions_delivered": 0,
        "fuel_ms": 0,
        "outbound_bytes": 100,
        "media_minutes": None,
        "recorded_at": datetime.now(UTC),
    }
    base.update(overrides)
    await install_dal.workstream_usage_hourly.async_insert(**base)


async def test_query_usage_with_no_rows_returns_an_empty_page(install_dal: Any) -> None:
    rows, total = await query_usage(install_dal, tenant_id=1)
    assert rows == []
    assert total == 0


async def test_query_usage_sums_two_correction_rows_for_the_same_natural_key(
    install_dal: Any,
) -> None:
    await _seed_row(install_dal, events=2, outbound_bytes=100)
    await _seed_row(install_dal, events=3, outbound_bytes=50)
    rows, total = await query_usage(install_dal, tenant_id=1)
    assert total == 1
    assert rows[0].events == 5
    assert rows[0].outbound_bytes == 150


async def test_query_usage_filters_by_community(install_dal: Any) -> None:
    await _seed_row(install_dal, workstream_id="ws-a", community_id=1)
    await _seed_row(install_dal, workstream_id="ws-b", community_id=2)
    rows, total = await query_usage(install_dal, tenant_id=1, community_id=1)
    assert total == 1
    assert rows[0].workstream_id == "ws-a"


async def test_query_usage_filters_by_workstream(install_dal: Any) -> None:
    await _seed_row(install_dal, workstream_id="ws-a")
    await _seed_row(install_dal, workstream_id="ws-b")
    rows, total = await query_usage(install_dal, tenant_id=1, workstream_id="ws-b")
    assert total == 1
    assert rows[0].workstream_id == "ws-b"


async def test_query_usage_filters_by_date_range(install_dal: Any) -> None:
    await _seed_row(
        install_dal, workstream_id="ws-early", hour=datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    )
    await _seed_row(
        install_dal, workstream_id="ws-late", hour=datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    )
    rows, total = await query_usage(
        install_dal,
        tenant_id=1,
        hour_from=datetime(2026, 9, 15, 0, 0, tzinfo=UTC),
        hour_to=datetime(2026, 9, 25, 0, 0, tzinfo=UTC),
    )
    assert total == 1
    assert rows[0].workstream_id == "ws-late"


async def test_query_usage_paginates(install_dal: Any) -> None:
    for i in range(5):
        await _seed_row(
            install_dal, workstream_id=f"ws-{i}", hour=datetime(2026, 9, 14, i, 0, tzinfo=UTC)
        )
    page1, total = await query_usage(install_dal, tenant_id=1, limit=2, offset=0)
    page2, _ = await query_usage(install_dal, tenant_id=1, limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.workstream_id for r in page1} != {r.workstream_id for r in page2}


async def test_query_usage_never_returns_another_tenants_rows(install_dal: Any) -> None:
    await _seed_row(install_dal, tenant_id=1, workstream_id="ws-mine")
    await _seed_row(install_dal, tenant_id=2, workstream_id="ws-other")
    rows, total = await query_usage(install_dal, tenant_id=1)
    assert total == 1
    assert rows[0].workstream_id == "ws-mine"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"stage": "nonsense"}, "invalid_stage"),
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 500}, "invalid_limit"),
        ({"offset": -1}, "invalid_offset"),
        (
            {
                "hour_from": datetime(2026, 9, 20, tzinfo=UTC),
                "hour_to": datetime(2026, 9, 1, tzinfo=UTC),
            },
            "invalid_date_range",
        ),
        ({"workstream_id": "   "}, "invalid_workstream_id"),
    ],
)
async def test_query_usage_rejects_every_invalid_filter(
    install_dal: Any, kwargs: dict[str, Any], code: str
) -> None:
    with pytest.raises(ApiError) as excinfo:
        await query_usage(install_dal, tenant_id=1, **kwargs)
    assert excinfo.value.code == code
