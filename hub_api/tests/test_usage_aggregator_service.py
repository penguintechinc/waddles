"""Tests for the waddles:usage consumer.

Parsing, aggregation, and the CronJob entrypoint (spec Sec5.12, D31).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from penguin_dal.table_proxy import TableProxy

from services.bundle_install_dal import raw_sql_rows
from services.usage_aggregator_service import (
    USAGE_CONSUMER_GROUP,
    USAGE_STREAM,
    AggregationResult,
    parse_usage_entry,
    run_usage_aggregation_batch,
)


def _entry(**overrides: str) -> dict[str, str]:
    base = {
        "tenant_id": "1",
        "community_id": "_tenant",
        "workstream_id": "ws-1",
        "stage": "ingest",
        "app_id": "",
        "hour": "2026-09-14T10:00:00+00:00",
        "events": "3",
        "invocations": "0",
        "host_calls": "0",
        "actions_delivered": "0",
        "fuel_ms": "0",
        "outbound_bytes": "512",
    }
    base.update(overrides)
    return base


def test_parse_usage_entry_decodes_a_well_formed_entry() -> None:
    delta = parse_usage_entry(_entry())
    assert delta.tenant_id == 1
    assert delta.community_id is None
    assert delta.workstream_id == "ws-1"
    assert delta.stage == "ingest"
    assert delta.app_id is None
    assert delta.events == 3
    assert delta.media_minutes is None


def test_parse_usage_entry_decodes_byte_fields() -> None:
    raw = {k.encode(): v.encode() for k, v in _entry().items()}
    delta = parse_usage_entry(raw)
    assert delta.tenant_id == 1


def test_parse_usage_entry_parses_a_real_community_id() -> None:
    delta = parse_usage_entry(_entry(community_id="7", app_id="waddles.socials.music.default"))
    assert delta.community_id == 7
    assert delta.app_id == "waddles.socials.music.default"


def test_parse_usage_entry_parses_media_minutes_for_streaming() -> None:
    delta = parse_usage_entry(_entry(stage="streaming", media_minutes="4.5"))
    assert delta.media_minutes == 4.5


def test_parse_usage_entry_rejects_an_unknown_stage() -> None:
    with pytest.raises(ValueError, match="malformed"):
        parse_usage_entry(_entry(stage="nope"))


def test_parse_usage_entry_rejects_a_missing_required_field() -> None:
    entry = _entry()
    del entry["tenant_id"]
    with pytest.raises(ValueError, match="malformed"):
        parse_usage_entry(entry)


def test_parse_usage_entry_rejects_a_non_numeric_field() -> None:
    with pytest.raises(ValueError, match="malformed"):
        parse_usage_entry(_entry(events="not-a-number"))


async def test_run_usage_aggregation_batch_writes_one_row_per_group(install_dal: Any) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (
            USAGE_STREAM,
            [
                (
                    b"1-1",
                    {
                        b"tenant_id": b"1",
                        b"community_id": b"_tenant",
                        b"workstream_id": b"ws-1",
                        b"stage": b"ingest",
                        b"app_id": b"",
                        b"hour": b"2026-09-14T10:00:00+00:00",
                        b"events": b"2",
                        b"invocations": b"0",
                        b"host_calls": b"0",
                        b"actions_delivered": b"0",
                        b"fuel_ms": b"0",
                        b"outbound_bytes": b"100",
                    },
                ),
                (
                    b"1-2",
                    {
                        b"tenant_id": b"1",
                        b"community_id": b"_tenant",
                        b"workstream_id": b"ws-1",
                        b"stage": b"ingest",
                        b"app_id": b"",
                        b"hour": b"2026-09-14T10:00:00+00:00",
                        b"events": b"3",
                        b"invocations": b"0",
                        b"host_calls": b"0",
                        b"actions_delivered": b"0",
                        b"fuel_ms": b"0",
                        b"outbound_bytes": b"50",
                    },
                ),
            ],
        ),
    ]
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=2, written=1, skipped=0)
    rows = await raw_sql_rows(
        install_dal,
        "SELECT events, outbound_bytes FROM workstream_usage_hourly WHERE workstream_id = :w",
        {"w": "ws-1"},
    )
    row = rows.first()
    assert row["events"] == 5
    assert row["outbound_bytes"] == 150
    redis_client.xack.assert_called_once_with(USAGE_STREAM, USAGE_CONSUMER_GROUP, b"1-1", b"1-2")


async def test_run_usage_aggregation_batch_acks_and_skips_a_malformed_entry(
    install_dal: Any,
) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (USAGE_STREAM, [(b"2-1", {b"tenant_id": b"1", b"stage": b"bogus"})]),
    ]
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=1, written=0, skipped=1)
    redis_client.xack.assert_called_once_with(USAGE_STREAM, USAGE_CONSUMER_GROUP, b"2-1")


async def test_run_usage_aggregation_batch_with_nothing_to_read_examines_zero(
    install_dal: Any,
) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = []
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=0, written=0, skipped=0)
    redis_client.xack.assert_not_called()


async def test_run_usage_aggregation_batch_groups_two_different_workstreams_separately(
    install_dal: Any,
) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (
            USAGE_STREAM,
            [
                (
                    b"3-1",
                    {
                        b"tenant_id": b"1",
                        b"community_id": b"_tenant",
                        b"workstream_id": b"ws-a",
                        b"stage": b"process",
                        b"app_id": b"waddles.socials.music.default",
                        b"hour": b"2026-09-14T11:00:00+00:00",
                        b"events": b"1",
                        b"invocations": b"1",
                        b"host_calls": b"0",
                        b"actions_delivered": b"0",
                        b"fuel_ms": b"10",
                        b"outbound_bytes": b"0",
                    },
                ),
                (
                    b"3-2",
                    {
                        b"tenant_id": b"1",
                        b"community_id": b"_tenant",
                        b"workstream_id": b"ws-b",
                        b"stage": b"process",
                        b"app_id": b"waddles.socials.music.default",
                        b"hour": b"2026-09-14T11:00:00+00:00",
                        b"events": b"1",
                        b"invocations": b"1",
                        b"host_calls": b"0",
                        b"actions_delivered": b"0",
                        b"fuel_ms": b"20",
                        b"outbound_bytes": b"0",
                    },
                ),
            ],
        ),
    ]
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=2, written=2, skipped=0)


async def test_run_usage_aggregation_batch_isolates_a_poison_group(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poison group's insert failure must not block or duplicate every other group in the batch.

    An FK violation on one group must not block acking (or cause
    re-insertion on a future retry) of every other group in the same
    batch, and must not leave the poison group's own entries stuck in
    the pending-entries list forever.

    `install_dal.workstream_usage_hourly` is a fresh `TableProxy` on every
    attribute access (`AsyncDB.__getattr__` builds a new one each time,
    never caching per-instance) -- patching one such throwaway instance
    would never affect the one `run_usage_aggregation_batch()` itself
    creates. Patching `TableProxy.async_insert` at the class level, gated
    on this table name and the poison `tenant_id`, is what actually
    intercepts the real call.
    """
    real_async_insert = TableProxy.async_insert

    async def _poison_insert(self: Any, **kwargs: Any) -> Any:
        if self._table.name == "workstream_usage_hourly" and kwargs.get("tenant_id") == 999:
            raise RuntimeError("simulated FK violation")
        return await real_async_insert(self, **kwargs)

    monkeypatch.setattr(TableProxy, "async_insert", _poison_insert)

    def _fields(tenant_id: bytes, workstream_id: bytes, events: bytes) -> dict[bytes, bytes]:
        return {
            b"tenant_id": tenant_id,
            b"community_id": b"_tenant",
            b"workstream_id": workstream_id,
            b"stage": b"ingest",
            b"app_id": b"",
            b"hour": b"2026-09-14T10:00:00+00:00",
            b"events": events,
            b"invocations": b"0",
            b"host_calls": b"0",
            b"actions_delivered": b"0",
            b"fuel_ms": b"0",
            b"outbound_bytes": b"0",
        }

    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (
            USAGE_STREAM,
            [
                (b"4-1", _fields(b"1", b"ws-good", b"2")),
                (b"4-2", _fields(b"999", b"ws-poison", b"9")),
            ],
        ),
    ]

    result = await run_usage_aggregation_batch(install_dal, redis_client)

    assert result == AggregationResult(examined=2, written=1, skipped=1)
    # Both groups get ack'd -- the poison group is dead-lettered, not left
    # stuck pending forever.
    acked = {eid for call in redis_client.xack.call_args_list for eid in call.args[2:]}
    assert acked == {b"4-1", b"4-2"}
    good_rows = await raw_sql_rows(
        install_dal,
        "SELECT events FROM workstream_usage_hourly WHERE workstream_id = :w",
        {"w": "ws-good"},
    )
    assert good_rows.first()["events"] == 2
    poison_rows = await raw_sql_rows(
        install_dal,
        "SELECT events FROM workstream_usage_hourly WHERE workstream_id = :w",
        {"w": "ws-poison"},
    )
    assert poison_rows.first() is None


async def test_main_propagates_a_redis_connection_error(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    """Prove the gate can fail.

    A Redis outage must not be swallowed into a silent zero-examined pass.
    """
    from services import usage_aggregator_service as job

    monkeypatch.setenv("DATABASE_URL", "sqlite://usage-aggregator-test.db")

    async def _raise_on_read(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("valkey unreachable")

    mock_client = AsyncMock()
    mock_client.xreadgroup.side_effect = _raise_on_read
    with (
        patch.object(job, "build_client", return_value=mock_client),
        patch.object(job, "ensure_group", new_callable=AsyncMock),
        patch.object(job, "_build_install_dal", new_callable=AsyncMock, return_value=install_dal),
        pytest.raises(ConnectionError),
    ):
        await job.main()
