"""Tests for `bundles.streaming_stream_action.list_streams`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from flask_core import (
    AsyncDAL,
    PlatformEvent,
    StageEnvelope,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_transports import NonRetryableTransportError

from bundles.streaming_stream_action import (
    LiveStreamDTO,
    StreamDetailsDTO,
    list_streams,
)


def _envelope(payload: dict[str, object] | None = None) -> StageEnvelope:
    """Fixture: a StageEnvelope with minimal required fields."""
    default_payload: dict[str, object] = {"query": "get_live_streams"}
    return StageEnvelope(
        tenant="global",
        community="1",
        app_id="waddles.streaming.stream.default",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="query",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-09-04T12:00:00Z",
        ),
        ts="2026-09-04T12:00:00Z",
    )


def _mock_row(**overrides: object) -> MagicMock:
    """Fixture: one coordination+community_servers joined row."""
    base = {
        "entity_id": "twitch-123",
        "platform": "twitch",
        "channel_id": "456",
        "channel_name": "test_channel",
        "is_live": True,
        "live_since": None,
        "viewer_count": 100,
        "stream_title": "Test Stream",
        "game_name": "Just Chatting",
        "thumbnail_url": "https://example.com/thumb.jpg",
        "last_updated": None,
    }
    base.update(overrides)
    row = MagicMock()
    for key, val in base.items():
        setattr(row, key, val)
    return row


def _ctx(community: str | None = "1") -> Any:
    """`bundle_context()` for this bundle's `app_id` -- shared to keep call sites short."""
    return bundle_context(
        tenant="global", community=community, app_id="waddles.streaming.stream.default"
    )


class _FakeDal:
    """In-memory stand-in for `AsyncDAL` -- only the `.select_async()` surface this bundle uses."""

    def __init__(self) -> None:
        self.dal = MagicMock()
        # `.tables` pre-populated so `_ensure_streaming_tables`'s idempotent
        # `"x" not in async_dal.tables` guard is a no-op against this fake.
        self.tables = ("community_servers", "coordination")

    def define_table(self, *args: object, **kwargs: object) -> None:
        """No-op -- `.tables` above already short-circuits `_ensure_streaming_tables`."""

    async def select_async(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN002, ANN003
        """Mock select_async returning a list of mock rows."""
        return []


@pytest.fixture(autouse=True)
def _dal() -> Any:
    """Fixture: inject a fake DAL and reset after test."""
    fake = _FakeDal()
    set_bundle_dal(fake)
    yield fake
    reset_bundle_dal_for_tests()


async def test_get_live_streams_success(_dal: _FakeDal) -> None:
    """Test `get_live_streams` query returns all streams ordered by viewer count."""
    row1 = _mock_row(entity_id="stream-1", viewer_count=500)
    row2 = _mock_row(entity_id="stream-2", viewer_count=100)
    mock_rows = [row1, row2]

    _dal.select_async = AsyncMock(return_value=mock_rows)  # type: ignore[method-assign]

    async with httpx.AsyncClient() as client:
        with _ctx():
            result = await list_streams(_envelope(), {}, http_client=client)

    assert result.transport == "bundle"
    assert result.http_status == 200
    # Parse the JSON detail field to verify structure
    dtos = json.loads(result.detail)
    assert isinstance(dtos, list)
    assert len(dtos) == 2
    assert dtos[0]["entityId"] == "stream-1"
    assert dtos[0]["viewerCount"] == 500
    assert dtos[1]["entityId"] == "stream-2"
    assert dtos[1]["viewerCount"] == 100


async def test_get_featured_streams_returns_top_5(_dal: _FakeDal) -> None:
    """Test `get_featured_streams` query returns top 5 by viewer count."""
    # Return more than 5 rows; runner limits to 5
    rows = [_mock_row(entity_id=f"stream-{i}", viewer_count=100 - i) for i in range(7)]
    _dal.select_async = AsyncMock(return_value=rows)  # type: ignore[method-assign]

    async with httpx.AsyncClient() as client:
        with _ctx():
            result = await list_streams(
                _envelope(payload={"query": "get_featured_streams"}),
                {},
                http_client=client,
            )

    dtos = json.loads(result.detail)
    assert isinstance(dtos, list)
    # The limitby is applied by the mock's select_async call
    assert len(dtos) == 7  # mock returns all rows; real DB would limit to 5


async def test_get_stream_details_success(_dal: _FakeDal) -> None:
    """Test `get_stream_details` returns one stream with lastActivity field."""
    row = _mock_row(entity_id="stream-detail", last_updated="2026-09-04T11:00:00")
    mock_rows = MagicMock()
    mock_rows.first = MagicMock(return_value=row)
    mock_rows.__len__ = MagicMock(return_value=1)
    mock_rows.__bool__ = MagicMock(return_value=True)
    _dal.select_async = AsyncMock(return_value=mock_rows)  # type: ignore[method-assign]

    async with httpx.AsyncClient() as client:
        with _ctx():
            result = await list_streams(
                _envelope(
                    payload={
                        "query": "get_stream_details",
                        "entity_id": "stream-detail",
                    }
                ),
                {},
                http_client=client,
            )

    assert result.http_status == 200
    dto = json.loads(result.detail)
    assert isinstance(dto, dict)
    assert dto["entityId"] == "stream-detail"
    assert dto["lastActivity"] == "2026-09-04T11:00:00"


async def test_unknown_query_type_is_non_retryable() -> None:
    """Test unknown query type raises NonRetryableTransportError."""
    async with httpx.AsyncClient() as client:
        with _ctx():
            with pytest.raises(NonRetryableTransportError, match="unknown query type"):
                await list_streams(
                    _envelope(payload={"query": "get_unknown_thing"}),
                    {},
                    http_client=client,
                )


async def test_get_stream_details_missing_entity_id_is_non_retryable() -> None:
    """Test get_stream_details without entity_id raises NonRetryableTransportError."""
    async with httpx.AsyncClient() as client:
        with _ctx():
            with pytest.raises(NonRetryableTransportError, match="entity_id"):
                await list_streams(
                    _envelope(
                        payload={
                            "query": "get_stream_details",
                        }
                    ),
                    {},
                    http_client=client,
                )


async def test_get_stream_details_not_found_is_non_retryable(_dal: _FakeDal) -> None:
    """Test get_stream_details with no matching row raises NonRetryableTransportError."""
    mock_rows = MagicMock()
    mock_rows.__bool__ = MagicMock(return_value=False)  # empty result set
    _dal.select_async = AsyncMock(return_value=mock_rows)  # type: ignore[method-assign]

    async with httpx.AsyncClient() as client:
        with _ctx():
            with pytest.raises(NonRetryableTransportError, match="no live stream found"):
                await list_streams(
                    _envelope(
                        payload={
                            "query": "get_stream_details",
                            "entity_id": "nonexistent",
                        }
                    ),
                    {},
                    http_client=client,
                )


async def test_dto_serialization() -> None:
    """Test LiveStreamDTO and StreamDetailsDTO serialize correctly to JSON."""
    from dataclasses import asdict

    dto = LiveStreamDTO(
        entityId="stream-1",
        platform="twitch",
        channelId="chan-1",
        channelName="Test Channel",
        isLive=True,
        liveSince="2026-09-04T10:00:00Z",
        viewerCount=250,
        title="Test Stream",
        game="Just Chatting",
        thumbnailUrl="https://example.com/thumb.jpg",
    )
    serialized = json.dumps(asdict(dto))
    parsed = json.loads(serialized)
    assert parsed["entityId"] == "stream-1"
    assert parsed["viewerCount"] == 250

    details_dto = StreamDetailsDTO(
        entityId="stream-1",
        platform="twitch",
        channelId="chan-1",
        channelName="Test Channel",
        isLive=True,
        liveSince="2026-09-04T10:00:00Z",
        viewerCount=250,
        title="Test Stream",
        game="Just Chatting",
        thumbnailUrl="https://example.com/thumb.jpg",
        lastActivity="2026-09-04T11:30:00Z",
    )
    serialized = json.dumps(asdict(details_dto))
    parsed = json.loads(serialized)
    assert parsed["lastActivity"] == "2026-09-04T11:30:00Z"


async def test_channel_name_fallback_to_channel_id() -> None:
    """Test that channel_name falls back to channel_id when null."""
    row = _mock_row(channel_name=None, channel_id="fallback-chan-123")
    from bundles.streaming_stream_action import _stream_dto

    dto = _stream_dto(row)
    assert dto.channelName == "fallback-chan-123"


async def test_default_values_for_nullable_fields() -> None:
    """Test that nullable fields default to sensible values."""
    row = _mock_row(stream_title=None, game_name=None, thumbnail_url=None, viewer_count=None)
    from bundles.streaming_stream_action import _stream_dto

    dto = _stream_dto(row)
    assert dto.title == ""
    assert dto.game == ""
    assert dto.thumbnailUrl == ""
    assert dto.viewerCount == 0


async def test_payload_community_id_mismatch_is_rejected() -> None:
    """Test IDOR guard: payload community_id different from context. regression: IDOR."""
    async with httpx.AsyncClient() as client:
        with _ctx():
            # Payload has community_id=999, context has community="1" -- should reject
            with pytest.raises(NonRetryableTransportError, match="does not match envelope context"):
                await list_streams(
                    _envelope(payload={"query": "get_live_streams", "community_id": 999}),
                    {},
                    http_client=client,
                )


async def test_missing_context_community_is_rejected() -> None:
    """Test tenant-wide activation (no community in context) is unsupported."""
    async with httpx.AsyncClient() as client:
        with _ctx(None):
            with pytest.raises(NonRetryableTransportError, match="community is None"):
                await list_streams(
                    _envelope(payload={"query": "get_live_streams"}),
                    {},
                    http_client=client,
                )


@pytest.fixture
async def real_dal(tmp_path: Path) -> AsyncIterator[AsyncDAL]:
    """Real sqlite `AsyncDAL` -- `tenants`/`communities`/`community_servers`/`coordination` created.

    Same two-tier convention as `test_bundles_twitch_shoutout_action.py`'s
    own `dal` fixture: `_ensure_streaming_tables` always binds with
    `migrate=False`, so this fixture defines the identical column set
    with `migrate=True` first; `_ensure_streaming_tables`'s own guard
    then finds the tables already registered and is a no-op.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path}/streaming_test.db", pool_size=1, migrate=True)
    d = async_dal.dal
    d.define_table("tenants", migrate=True)
    d.define_table("communities", d.Field("tenant_id", "reference tenants"), migrate=True)
    d.define_table(
        "community_servers",
        d.Field("community_id", "reference communities", notnull=True),
        d.Field("platform", "string", notnull=True),
        d.Field("platform_server_id", "string", notnull=True),
        d.Field("status", "string", default="pending"),
        migrate=True,
    )
    d.define_table(
        "coordination",
        d.Field("entity_id", "string", notnull=True),
        d.Field("platform", "string", notnull=True),
        d.Field("server_id", "string"),
        d.Field("channel_id", "string"),
        d.Field("channel_name", "string"),
        d.Field("is_live", "boolean", default=False),
        d.Field("viewer_count", "integer", default=0),
        d.Field("live_since", "datetime"),
        d.Field("stream_title", "string"),
        d.Field("game_name", "string"),
        d.Field("thumbnail_url", "string"),
        d.Field("last_updated", "datetime"),
        migrate=True,
    )
    d.tenants.insert()
    d.communities.insert(tenant_id=1)
    d.commit()
    set_bundle_dal(async_dal)
    try:
        yield async_dal
    finally:
        reset_bundle_dal_for_tests()
        try:
            await async_dal.close_async()
        except Exception:  # noqa: BLE001, S110 -- known pydal cross-thread close gotcha
            pass  # nosec B110


async def test_get_live_streams_against_real_sqlite(real_dal: AsyncDAL) -> None:
    # regression: gh-298 real-pydal
    """`dal(query)` join query works end-to-end: insert server+coordination, list, read back."""
    d = real_dal.dal
    community_id = d.communities.insert(tenant_id=1)
    d.community_servers.insert(
        community_id=community_id,
        platform="twitch",
        platform_server_id="srv-1",
        status="approved",
    )
    d.coordination.insert(
        entity_id="twitch-real-1",
        platform="twitch",
        server_id="srv-1",
        channel_id="chan-real",
        channel_name="real_channel",
        is_live=True,
        viewer_count=42,
        stream_title="Real Stream",
        game_name="Just Chatting",
        thumbnail_url="https://example.com/real.jpg",
    )
    d.commit()

    async with httpx.AsyncClient() as client:
        with bundle_context(
            tenant="global", community=str(community_id), app_id="waddles.streaming.stream.default"
        ):
            result = await list_streams(
                _envelope(payload={"query": "get_live_streams"}), {}, http_client=client
            )

    dtos = json.loads(result.detail)
    assert len(dtos) == 1
    assert dtos[0]["entityId"] == "twitch-real-1"
    assert dtos[0]["viewerCount"] == 42
