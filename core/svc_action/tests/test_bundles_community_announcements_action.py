"""Tests for `bundles.community_announcements_action.broadcast_announcement`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from flask_core import (
    AsyncDAL,
    PlatformEvent,
    StageEnvelope,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from bundles.community_announcements_action import broadcast_announcement


def _envelope(
    platform: str = "discord",
    target_platforms: list[str] | None = None,
    announcement_id: int = 42,
    **payload_overrides: object,
) -> StageEnvelope:
    """Build a test StageEnvelope with announcement data."""
    if target_platforms is None:
        target_platforms = ["discord"]

    announcement_data = {
        "id": announcement_id,
        "title": "Test Announcement",
        "content": "Test content",
        "announcement_type": "general",
        "status": "published",
        "community_id": 1,
    }
    payload = {
        "text": "announcement broadcast",
        "channel_id": "general",
        "announcement": announcement_data,
        "target_platforms": target_platforms,
        "announcement_id": announcement_id,
    }
    payload.update(payload_overrides)

    return StageEnvelope(
        tenant="1",
        community="1",
        app_id="waddles.community.announcements.default",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="message",
            actor=None,
            payload=payload,
            occurred_at="2026-09-04T00:00:00Z",
        ),
        ts="2026-09-04T00:00:00Z",
    )


def _config(**overrides: object) -> dict:
    """Build a test config dict."""
    base = {
        "discord_endpoint": "http://8.8.8.8:8070",
        "twitch_endpoint": "http://8.8.8.8:8072",
    }
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    """Build an AsyncClient with a MockTransport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class _FakeDalQuery:
    """Fake query object supporting chaining."""

    def __init__(self, parent_dal: Any) -> None:
        self._parent_dal = parent_dal
        self._community_id: int | None = None
        self._platforms: list[str] | None = None

    def __and__(self, other: Any) -> Any:
        """Support & operator for combining queries."""
        # other should be a platform filter, extract info if possible
        return self

    def __eq__(self, other: int) -> Any:
        """Support == comparison for community_id."""
        self._parent_dal._query_community_id = other
        return self

    def select(self) -> list[Any]:
        """Return servers matching the query."""
        if self._parent_dal._query_community_id is None:
            return []
        result = []
        for s in self._parent_dal._servers:
            if s["community_id"] != self._parent_dal._query_community_id:
                continue
            if self._parent_dal._query_platforms:
                if s["platform"] not in self._parent_dal._query_platforms:
                    continue
            result.append(type("Server", (), s)())
        return result

    async def __aenter__(self):
        """Support async context manager pattern if needed."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Support async context manager pattern if needed."""
        pass


class _FakeDalTable:
    """Fake table object supporting community_servers and announcement_broadcasts access."""

    def __init__(self, parent_dal: Any) -> None:
        self._parent_dal = parent_dal

    def __getattr__(self, name: str) -> Any:
        """Support attribute access like community_id or platform."""
        if name == "community_id":
            return self
        elif name == "platform":
            return self
        return self

    def __eq__(self, other: int) -> Any:
        """Support == comparison."""
        self._parent_dal._query_community_id = other
        return _FakeDalQuery(self._parent_dal)

    def belongs(self, platforms: list[str]) -> Any:
        """Support .belongs() filter."""
        self._parent_dal._query_platforms = platforms
        return self

    def __and__(self, other: Any) -> Any:
        """Support & operator."""
        return self


class _FakeDal:
    """In-memory stand-in for AsyncDAL -- implements only the surface this bundle uses."""

    def __init__(self) -> None:
        self._servers: list[dict[str, Any]] = []
        self._query_community_id: int | None = None
        self._query_platforms: list[str] | None = None
        self.community_servers = _FakeDalTable(self)
        self.announcement_broadcasts = self
        # `dal.dal(query)` (the real `AsyncDAL.dal` raw-pydal-DAL attribute)
        # resolves to this same fake's own `__call__` -- and `.tables`
        # pre-populated so `_ensure_announcement_tables`'s idempotent
        # `"x" not in dal.tables` guard is a no-op against this fake.
        self.dal = self
        self.tables = ("community_servers", "announcement_broadcasts")

    def __call__(self, query: Any) -> Any:
        """Support the query pattern dal(dal.community_servers.community_id == id)."""
        return query if hasattr(query, "select") else self

    def insert(self, **kwargs: object) -> None:
        """Record a broadcast attempt."""
        if not hasattr(self, "_broadcasts"):
            self._broadcasts: list[Any] = []
        self._broadcasts.append(kwargs)

    def commit(self) -> None:
        """No-op for test."""
        pass

    async def select_async(self, query: Any) -> list[Any]:
        """Async version of select - delegates to sync select if query has it."""
        if hasattr(query, "select"):
            result = query.select()
            return result
        # For complex queries, return empty (tests will use mocks for complex cases)
        return []

    async def insert_async(self, table: Any, **kwargs: object) -> None:
        """Async version of insert - delegates to sync insert."""
        self.insert(**kwargs)

    def add_server(self, id: int, platform: str, community_id: int = 1) -> None:
        """Add a test server."""
        self._servers.append({
            "id": id,
            "platform": platform,
            "community_id": community_id,
        })

    def set_servers_empty(self) -> None:
        """Clear all servers."""
        self._servers = []


@pytest.fixture(autouse=True)
def _dal() -> Any:
    """Inject fake DAL and reset after each test."""
    fake = _FakeDal()
    set_bundle_dal(fake)
    yield fake
    reset_bundle_dal_for_tests()


class TestBroadcastAnnouncement:
    """Tests for announcement broadcast to platforms."""

    async def test_broadcasts_to_single_platform(self, _dal: _FakeDal) -> None:
        """Test broadcasting an announcement to a single platform."""
        _dal.add_server(1, "discord", community_id=1)

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/internal/announce" in request.url.path
            return httpx.Response(200, json={"success": True})

        async with _client(handler) as client:
            with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                "discord": "http://8.8.8.8:8070",
                "slack": "http://8.8.8.8:8071",
                "twitch": "http://8.8.8.8:8072",
                "youtube": "http://8.8.8.8:8073",
            }):
                result = await broadcast_announcement(_envelope(), _config(), http_client=client)

        assert result.transport == "bundle"
        assert "1/1" in result.detail

    @pytest.mark.asyncio
    async def test_broadcasts_to_multiple_platforms(self) -> None:
        """Test broadcasting to multiple platforms."""
        mock_dal = mock.MagicMock()
        mock_discord_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_twitch_server = mock.MagicMock(id=2, platform="twitch", community_id=1)
        mock_dal.select_async = mock.AsyncMock(return_value=[
            mock_discord_server,
            mock_twitch_server,
        ])
        mock_dal.insert_async = mock.AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with _client(handler) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                    "discord": "http://8.8.8.8:8070",
                    "slack": "http://8.8.8.8:8071",
                    "twitch": "http://8.8.8.8:8072",
                    "youtube": "http://8.8.8.8:8073",
                }):
                    result = await broadcast_announcement(
                        _envelope(target_platforms=["discord", "twitch"]),
                        _config(),
                        http_client=client,
                    )

        assert result.transport == "bundle"
        assert "2/2" in result.detail

    @pytest.mark.asyncio
    async def test_records_broadcast_results_in_db(self) -> None:
        """Test that broadcast attempts are recorded in announcement_broadcasts."""
        mock_dal = mock.MagicMock()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.select_async = mock.AsyncMock(return_value=[mock_server])
        mock_dal.insert_async = mock.AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async with _client(handler) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                    "discord": "http://8.8.8.8:8070",
                    "slack": "http://8.8.8.8:8071",
                    "twitch": "http://8.8.8.8:8072",
                    "youtube": "http://8.8.8.8:8073",
                }):
                    await broadcast_announcement(_envelope(), _config(), http_client=client)

        # Verify insert was called with correct parameters
        mock_dal.insert_async.assert_called()
        call_args = mock_dal.insert_async.call_args
        assert call_args[1]["announcement_id"] == 42
        assert call_args[1]["community_server_id"] == 1
        assert call_args[1]["platform"] == "discord"
        assert call_args[1]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_handles_partial_platform_failure(self) -> None:
        """Test when some platforms succeed and others fail."""
        mock_dal = mock.MagicMock()
        mock_discord_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_twitch_server = mock.MagicMock(id=2, platform="twitch", community_id=1)
        mock_dal.select_async = mock.AsyncMock(return_value=[
            mock_discord_server,
            mock_twitch_server,
        ])
        mock_dal.insert_async = mock.AsyncMock()

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if "discord" in request.url.host or "8070" in str(request.url):
                return httpx.Response(200)
            else:
                return httpx.Response(500)  # Twitch fails

        async with _client(handler) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                    "discord": "http://8.8.8.8:8070",
                    "slack": "http://8.8.8.8:8071",
                    "twitch": "http://8.8.8.8:8072",
                    "youtube": "http://8.8.8.8:8073",
                }):
                    with pytest.raises(NonRetryableTransportError, match="partial failure"):
                        await broadcast_announcement(
                            _envelope(target_platforms=["discord", "twitch"]),
                            _config(),
                            http_client=client,
                        )

    @pytest.mark.asyncio
    async def test_all_failures_is_retryable(self) -> None:
        """Test that all platforms failing is retryable (network issue)."""
        mock_dal = mock.MagicMock()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.select_async = mock.AsyncMock(return_value=[mock_server])
        mock_dal.insert_async = mock.AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async with _client(handler) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                    "discord": "http://8.8.8.8:8070",
                    "slack": "http://8.8.8.8:8071",
                    "twitch": "http://8.8.8.8:8072",
                    "youtube": "http://8.8.8.8:8073",
                }):
                    with pytest.raises(RetryableTransportError, match="all .* servers failed"):
                        await broadcast_announcement(_envelope(), _config(), http_client=client)

    @pytest.mark.asyncio
    async def test_missing_announcement_data_is_non_retryable(self) -> None:
        """Test that missing announcement dict is non-retryable."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="announcement"):
                await broadcast_announcement(
                    _envelope(announcement="missing"),  # type: ignore
                    _config(),
                    http_client=client,
                )

    @pytest.mark.asyncio
    async def test_missing_target_platforms_is_non_retryable(self) -> None:
        """Test that missing target_platforms is non-retryable."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="target_platforms"):
                await broadcast_announcement(
                    _envelope(target_platforms=[]),
                    _config(),
                    http_client=client,
                )

    @pytest.mark.asyncio
    async def test_missing_announcement_id_is_non_retryable(self) -> None:
        """Test that missing announcement_id is non-retryable."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="announcement_id"):
                await broadcast_announcement(
                    _envelope(announcement_id="not_an_int"),  # type: ignore
                    _config(),
                    http_client=client,
                )

    @pytest.mark.asyncio
    async def test_missing_community_id_is_non_retryable(self) -> None:
        """Test that missing community_id is non-retryable."""
        envelope = _envelope()
        # Create a new envelope with community=None
        envelope = StageEnvelope(
            tenant=envelope.tenant,
            community=None,  # Missing community
            app_id=envelope.app_id,
            stage=envelope.stage,
            event=envelope.event,
            ts=envelope.ts,
        )
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="community_id"):
                await broadcast_announcement(envelope, _config(), http_client=client)

    @pytest.mark.asyncio
    async def test_no_servers_found_is_non_retryable(self) -> None:
        """Test that no matching servers is non-retryable."""
        mock_dal = mock.MagicMock()
        mock_dal.select_async = mock.AsyncMock(return_value=[])

        async with _client(lambda r: httpx.Response(200)) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with pytest.raises(NonRetryableTransportError, match="no active servers"):
                    await broadcast_announcement(_envelope(), _config(), http_client=client)

    @pytest.mark.asyncio
    async def test_network_timeout_is_retryable(self) -> None:
        """Test that network timeouts are retryable."""
        mock_dal = mock.MagicMock()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.select_async = mock.AsyncMock(return_value=[mock_server])
        mock_dal.insert_async = mock.AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timeout")

        async with _client(handler) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                    "discord": "http://8.8.8.8:8070",
                    "slack": "http://8.8.8.8:8071",
                    "twitch": "http://8.8.8.8:8072",
                    "youtube": "http://8.8.8.8:8073",
                }):
                    with pytest.raises(RetryableTransportError, match="all .* servers failed"):
                        await broadcast_announcement(_envelope(), _config(), http_client=client)

    @pytest.mark.asyncio
    async def test_preserves_announcement_data(self) -> None:
        """Test that announcement data is correctly passed to endpoint."""
        mock_dal = mock.MagicMock()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.select_async = mock.AsyncMock(return_value=[mock_server])
        mock_dal.insert_async = mock.AsyncMock()

        captured_body = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body["body"] = request.content.decode()
            return httpx.Response(200)

        async with _client(handler) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                    "discord": "http://8.8.8.8:8070",
                    "slack": "http://8.8.8.8:8071",
                    "twitch": "http://8.8.8.8:8072",
                    "youtube": "http://8.8.8.8:8073",
                }):
                    await broadcast_announcement(
                        _envelope(announcement_id=99),
                        _config(),
                        http_client=client,
                    )

        assert "Test Announcement" in captured_body["body"]
        assert "99" in captured_body["body"]


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_missing_platform_endpoint_returns_false(self) -> None:
        """Test that missing platform endpoint is handled gracefully."""
        from bundles.community_announcements_action import _post_to_platform

        client = httpx.AsyncClient()
        result = await _post_to_platform(client, "unknown_platform", {})

        assert result[0] is False
        assert "No action endpoint configured" in result[1]

    @pytest.mark.asyncio
    async def test_generic_exception_in_post_returns_error(self) -> None:
        """Test that generic exceptions are caught and returned as errors."""
        from bundles.community_announcements_action import _post_to_platform

        # Create a client that will raise an unexpected exception
        def handler(request):
            raise RuntimeError("Unexpected error")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
            "discord": "http://8.8.8.8:8070",
            "slack": "http://8.8.8.8:8071",
            "twitch": "http://8.8.8.8:8072",
            "youtube": "http://8.8.8.8:8073",
        }):
            result = await _post_to_platform(client, "discord", {})

        assert result[0] is False
        assert "Unexpected error" in result[1]


@pytest.fixture
async def real_dal(tmp_path: Path) -> AsyncIterator[AsyncDAL]:
    """Real sqlite `AsyncDAL` -- `tenants`/`communities`/`community_servers` physically created.

    `_ensure_announcement_tables` always binds `community_servers`/
    `announcement_broadcasts` with `migrate=False` (schema owned by
    `000_create_base_schema.sql`, assumed to already exist against real
    Postgres) -- a throwaway sqlite file has no such table until
    something actually creates it, so this fixture defines the
    identical column set with `migrate=True` first (same two-tier
    convention `test_bundles_twitch_shoutout_action.py`'s own `dal`
    fixture uses). `_ensure_announcement_tables`'s own `if "community_
    servers" not in dal.tables` guard then finds both tables already
    registered and is a no-op when the bundle runs.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path}/announcements_test.db", pool_size=1, migrate=True)
    d = async_dal.dal
    d.define_table("tenants", migrate=True)
    d.define_table("communities", d.Field("tenant_id", "reference tenants"), migrate=True)
    d.define_table(
        "community_servers",
        d.Field("community_id", "reference communities", notnull=True),
        d.Field("platform", "string", notnull=True),
        migrate=True,
    )
    d.define_table(
        "announcement_broadcasts",
        d.Field("announcement_id", "integer", notnull=True),
        d.Field("community_server_id", "integer"),
        d.Field("platform", "string", notnull=True),
        d.Field("status", "string", default="pending"),
        d.Field("error_message", "string"),
        d.Field("broadcasted_at", "datetime"),
        d.Field("created_at", "datetime"),
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


class TestRealPydal:
    """Real-DB smoke (gh-298): exercises the actual pydal query path, not a fake/mock."""

    async def test_broadcast_against_real_sqlite_inserts_and_records(
        self, real_dal: AsyncDAL
    ) -> None:
        # regression: gh-298 real-pydal
        """`dal.dal(query)` fix: insert one community_server, broadcast, read back the audit row."""
        d = real_dal.dal
        server_id = d.community_servers.insert(community_id=1, platform="discord")
        d.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True})

        async with _client(handler) as client:
            with mock.patch("bundles.community_announcements_action._PLATFORM_ENDPOINTS", {
                "discord": "http://8.8.8.8:8070",
                "slack": "http://8.8.8.8:8071",
                "twitch": "http://8.8.8.8:8072",
                "youtube": "http://8.8.8.8:8073",
            }):
                result = await broadcast_announcement(_envelope(), _config(), http_client=client)

        assert result.transport == "bundle"
        assert "1/1" in result.detail

        broadcasts = d(d.announcement_broadcasts.community_server_id == server_id).select()
        assert len(broadcasts) == 1
        assert broadcasts[0].platform == "discord"
        assert broadcasts[0].status == "sent"
        assert broadcasts[0].announcement_id == 42
