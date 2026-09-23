"""hub-api's `waddles:usage` consumer -- writes workstream_usage_hourly.

Spec Sec5.12, Sec6.12, D31.

Stages (`svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`) are
XADD-only producers onto `waddles:usage` (spec Sec11.10.2) -- hub-api is
the one and only reader, through its own consumer group. Every batch is
committed to Postgres BEFORE the entries it covers are XACKed, so a
crash between commit and XACK causes at-least-once redelivery (a
Postgres row gets written twice on the next run) rather than
at-most-once data loss -- acceptable per spec Sec5.12 ("no charging,
quota or enforcement ships now") and consistent with
`workstream_usage_hourly` being append-only/summed-at-query-time by
design: a duplicated correction row is exactly the shape the table
already tolerates.

Wire shape of one `waddles:usage` XADD entry (this task's own decision;
must match whichever plan implements the stage-side producer -- M3
svc_action, M4 svc_process, M5 svc_ingest, and svc_streaming's own
future plan): `tenant_id` (decimal string), `community_id` (decimal
string, or the literal "_tenant" for tenant-wide), `workstream_id`
(opaque string), `stage` (one of ingest/process/action/streaming),
`app_id` (string, or "" for ingest-stage entries with no bundle),
`hour` (RFC3339, truncated to the hour), `events`/`invocations`/
`host_calls`/`actions_delivered`/`fuel_ms`/`outbound_bytes` (decimal
strings, default "0"), `media_minutes` (decimal string, svc-streaming
only, absent elsewhere).

R52: `workstream_usage_hourly` is this slice's own new table, queried
through the penguin-dal `install_dal: AsyncDB`. This is a standalone
process (a Kubernetes CronJob), so it builds its own `install_dal` via
`build_install_dal()` rather than reading one from a Quart
`app.config`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_install_dal import build_install_dal
from services.bundle_telemetry import bundle_span, get_meter
from services.valkey_admin_client import build_client, ensure_group

USAGE_STREAM = "waddles:usage"
USAGE_CONSUMER_GROUP = "hub_api_usage_aggregator"
_TENANT_WIDE_SENTINEL = "_tenant"
_VALID_STAGES = frozenset({"ingest", "process", "action", "streaming"})

_meter = get_meter()
_batches_counter = _meter.create_counter(
    "waddles_hub_usage_batches_total", description="usage aggregator batch runs, by result"
)
_rows_written_histogram = _meter.create_histogram(
    "waddles_hub_usage_rows_written", description="workstream_usage_hourly rows written per batch"
)
_entries_skipped_counter = _meter.create_counter(
    "waddles_hub_usage_entries_skipped_total",
    description=(
        "waddles:usage entries acked without being written to "
        "workstream_usage_hourly (unparseable, or a poison group's insert failed)"
    ),
)


@dataclass(slots=True, frozen=True)
class UsageDelta:
    """One decoded `waddles:usage` entry."""

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


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def parse_usage_entry(fields: dict[Any, Any]) -> UsageDelta:
    """Decode one `XREADGROUP` entry's field map into a `UsageDelta`.

    Raises `ValueError` if malformed.
    """
    decoded = {_decode(k): _decode(v) for k, v in fields.items()}
    try:
        stage = decoded["stage"]
        if stage not in _VALID_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        community_raw = decoded.get("community_id", _TENANT_WIDE_SENTINEL)
        community_id = None if community_raw == _TENANT_WIDE_SENTINEL else int(community_raw)
        app_id = decoded.get("app_id") or None
        media_raw = decoded.get("media_minutes")
        return UsageDelta(
            tenant_id=int(decoded["tenant_id"]),
            community_id=community_id,
            workstream_id=decoded["workstream_id"],
            stage=stage,
            app_id=app_id,
            hour=datetime.fromisoformat(decoded["hour"]),
            events=int(decoded.get("events", "0")),
            invocations=int(decoded.get("invocations", "0")),
            host_calls=int(decoded.get("host_calls", "0")),
            actions_delivered=int(decoded.get("actions_delivered", "0")),
            fuel_ms=int(decoded.get("fuel_ms", "0")),
            outbound_bytes=int(decoded.get("outbound_bytes", "0")),
            media_minutes=float(media_raw) if media_raw is not None else None,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"malformed waddles:usage entry: {exc}") from exc


@dataclass(slots=True, frozen=True)
class AggregationResult:
    """What one batch did. `examined` is the denominator a zero-written run is judged against."""

    examined: int
    written: int
    skipped: int


_GroupKey = tuple[int, int | None, str, str, str | None, datetime]


def _group_key(delta: UsageDelta) -> _GroupKey:
    return (
        delta.tenant_id,
        delta.community_id,
        delta.workstream_id,
        delta.stage,
        delta.app_id,
        delta.hour,
    )


async def run_usage_aggregation_batch(
    install_dal: AsyncDB,
    redis_client: Any,
    *,
    batch_size: int = 500,
    consumer_name: str = "hub-api-1",
) -> AggregationResult:
    """One bounded read-aggregate-write-ack pass over `waddles:usage`.

    Never blocks (`BLOCK` is unused). Each group's insert is isolated: a
    poison group (e.g. an FK violation on a stale `tenant_id`) is caught,
    counted via `_entries_skipped_counter`, and its entries are ack'd on
    their own -- so one bad group can neither block acking of every other
    group in the batch (which, unfixed, meant that group's rows would be
    re-inserted -- duplicated -- on every future retry of this same batch)
    nor leave its own entries stuck in the pending-entries list forever
    (a permanently stalled consumer for those entries).
    """
    async with bundle_span("hub.usage.aggregate_batch", stream=USAGE_STREAM):
        response = await redis_client.xreadgroup(
            USAGE_CONSUMER_GROUP, consumer_name, {USAGE_STREAM: ">"}, count=batch_size
        )
        if not response:
            _batches_counter.add(1, {"result": "empty"})
            return AggregationResult(examined=0, written=0, skipped=0)

        entries = response[0][1]
        groups: dict[_GroupKey, list[UsageDelta]] = defaultdict(list)
        group_entry_ids: dict[_GroupKey, list[Any]] = defaultdict(list)
        unparseable_ids: list[Any] = []
        skipped = 0
        for entry_id, fields in entries:
            try:
                delta = parse_usage_entry(fields)
            except ValueError:
                skipped += 1
                _entries_skipped_counter.add(1, {"reason": "unparseable"})
                unparseable_ids.append(entry_id)
                continue
            key = _group_key(delta)
            groups[key].append(delta)
            group_entry_ids[key].append(entry_id)

        # Unparseable entries can never succeed on retry -- ack them
        # immediately, independent of how any group below fares.
        if unparseable_ids:
            await redis_client.xack(USAGE_STREAM, USAGE_CONSUMER_GROUP, *unparseable_ids)

        now = datetime.now(UTC)
        written = 0
        for key, deltas in groups.items():
            tenant_id, community_id, workstream_id, stage, app_id, hour = key
            entry_ids = group_entry_ids[key]
            try:
                await install_dal.workstream_usage_hourly.async_insert(
                    tenant_id=tenant_id,
                    community_id=community_id,
                    workstream_id=workstream_id,
                    stage=stage,
                    app_id=app_id,
                    hour=hour,
                    events=sum(d.events for d in deltas),
                    invocations=sum(d.invocations for d in deltas),
                    host_calls=sum(d.host_calls for d in deltas),
                    actions_delivered=sum(d.actions_delivered for d in deltas),
                    fuel_ms=sum(d.fuel_ms for d in deltas),
                    outbound_bytes=sum(d.outbound_bytes for d in deltas),
                    media_minutes=(
                        sum(d.media_minutes for d in deltas if d.media_minutes is not None)
                        if any(d.media_minutes is not None for d in deltas)
                        else None
                    ),
                    recorded_at=now,
                )
            # Isolate the poison group -- it must never block acking, or
            # cause re-insertion, of every other group in this batch.
            except Exception:  # noqa: BLE001
                skipped += len(deltas)
                _entries_skipped_counter.add(len(deltas), {"reason": "insert_failed"})
                await redis_client.xack(USAGE_STREAM, USAGE_CONSUMER_GROUP, *entry_ids)
                continue
            written += 1
            await redis_client.xack(USAGE_STREAM, USAGE_CONSUMER_GROUP, *entry_ids)

        _batches_counter.add(1, {"result": "processed"})
        _rows_written_histogram.record(written)
        return AggregationResult(examined=len(entries), written=written, skipped=skipped)


async def _build_install_dal() -> AsyncDB:
    """Open this standalone CronJob process's own penguin-dal connection (R52).

    Same DSN as the app, a separate pool.
    """
    return await build_install_dal(os.environ["DATABASE_URL"], pool_size=1)


async def main() -> int:
    """CronJob entrypoint.

    Ensures the consumer group exists, drains up to 20 batches, prints denominators.

    An idle stream (zero entries examined) is the expected steady state
    between bursts of traffic, not a failure. A genuine "pointed at the
    wrong place" failure here (an unreachable Valkey, a missing
    DATABASE_URL) raises an exception instead of returning a silent
    zero.
    """
    install_dal = await _build_install_dal()
    redis_client = build_client()
    await ensure_group(redis_client, stream=USAGE_STREAM, group=USAGE_CONSUMER_GROUP)

    total_examined = 0
    total_written = 0
    total_skipped = 0
    for _ in range(20):
        result = await run_usage_aggregation_batch(install_dal, redis_client)
        total_examined += result.examined
        total_written += result.written
        total_skipped += result.skipped
        if result.examined == 0:
            break

    print(
        f"usage aggregation: examined={total_examined} "
        f"written={total_written} skipped={total_skipped}"
    )
    if total_examined == 0:
        print(
            "usage aggregation: zero entries examined this run -- normal when the stream is idle "
            "between bursts of traffic",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
