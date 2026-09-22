"""Per-community admin usage query (spec Sec5.12, Sec6.12, D31) -- read-only, no charging/quota.

`workstream_usage_hourly` allows more than one row per natural key (a
correction is a new row, spec Sec6.12) -- this module sums by
`(tenant_id, community_id, workstream_id, stage, app_id, hour)` at
query time, exactly as the spec's design intends, rather than exposing
raw, possibly-duplicated rows to an admin.

R52: `workstream_usage_hourly` is this slice's own new table, queried
through the penguin-dal `install_dal: AsyncDB`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal import AsyncDB

from services.errors import ApiError

MAX_USAGE_PAGE_SIZE = 200
_VALID_STAGES = frozenset({"ingest", "process", "action", "streaming"})


@dataclass(slots=True, frozen=True)
class UsageRow:
    """One aggregated `(tenant, community, workstream, stage, app)` usage bucket for one hour."""

    tenant_id: int
    community_id: int | None
    workstream_id: str
    stage: str
    app_id: str | None
    hour: datetime
    events: int
    invocations: int
    host_calls: int
    actions_delivered: int
    fuel_ms: int
    outbound_bytes: int
    media_minutes: float | None


def _validate_filters(
    *,
    workstream_id: str | None,
    stage: str | None,
    hour_from: datetime | None,
    hour_to: datetime | None,
    limit: int,
    offset: int,
) -> None:
    if workstream_id is not None and not workstream_id.strip():
        raise ApiError("workstreamId must not be blank", 422, "invalid_workstream_id")
    if stage is not None and stage not in _VALID_STAGES:
        raise ApiError(f"stage must be one of {sorted(_VALID_STAGES)}", 422, "invalid_stage")
    if hour_from is not None and hour_to is not None and hour_from > hour_to:
        raise ApiError("from must not be after to", 422, "invalid_date_range")
    if limit < 1 or limit > MAX_USAGE_PAGE_SIZE:
        raise ApiError(f"limit must be between 1 and {MAX_USAGE_PAGE_SIZE}", 422, "invalid_limit")
    if offset < 0:
        raise ApiError("offset must not be negative", 422, "invalid_offset")


async def query_usage(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None = None,
    workstream_id: str | None = None,
    stage: str | None = None,
    app_id: str | None = None,
    hour_from: datetime | None = None,
    hour_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[UsageRow], int]:
    """The tenant's usage, optionally scoped to one community, summed by natural key, paginated.

    Aggregation and pagination both happen in Python after the filtered
    rows are fetched -- an admin reporting view bounded by tenant (and
    usually by a community/date-range filter too), never a hot path, so
    this trades a little raw-row overhead for a query `penguin_dal`'s
    query-builder can express without a second, aggregate-specific
    code path (never raw SQL).
    """
    _validate_filters(
        workstream_id=workstream_id,
        stage=stage,
        hour_from=hour_from,
        hour_to=hour_to,
        limit=limit,
        offset=offset,
    )
    query = install_dal.workstream_usage_hourly.tenant_id == tenant_id
    if community_id is not None:
        query &= install_dal.workstream_usage_hourly.community_id == community_id
    if workstream_id is not None:
        query &= install_dal.workstream_usage_hourly.workstream_id == workstream_id
    if stage is not None:
        query &= install_dal.workstream_usage_hourly.stage == stage
    if app_id is not None:
        query &= install_dal.workstream_usage_hourly.app_id == app_id
    if hour_from is not None:
        query &= install_dal.workstream_usage_hourly.hour >= hour_from
    if hour_to is not None:
        query &= install_dal.workstream_usage_hourly.hour < hour_to

    raw_rows = await install_dal(query).select()

    grouped: dict[tuple[int, int | None, str, str, str | None, datetime], dict[str, Any]] = {}
    for row in raw_rows:
        key = (row.tenant_id, row.community_id, row.workstream_id, row.stage, row.app_id, row.hour)
        bucket = grouped.setdefault(
            key,
            {
                "events": 0,
                "invocations": 0,
                "host_calls": 0,
                "actions_delivered": 0,
                "fuel_ms": 0,
                "outbound_bytes": 0,
                "media_minutes": None,
            },
        )
        bucket["events"] += row.events
        bucket["invocations"] += row.invocations
        bucket["host_calls"] += row.host_calls
        bucket["actions_delivered"] += row.actions_delivered
        bucket["fuel_ms"] += row.fuel_ms
        bucket["outbound_bytes"] += row.outbound_bytes
        if row.media_minutes is not None:
            bucket["media_minutes"] = (bucket["media_minutes"] or 0) + row.media_minutes

    results = [
        UsageRow(
            tenant_id=key[0],
            community_id=key[1],
            workstream_id=key[2],
            stage=key[3],
            app_id=key[4],
            hour=key[5],
            **bucket,
        )
        for key, bucket in sorted(
            grouped.items(), key=lambda item: (item[0][5], item[0][2], item[0][3])
        )
    ]
    total = len(results)
    return results[offset : offset + limit], total
