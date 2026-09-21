"""Tests for `bundles.community_announcements_action.broadcast_announcement`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx
import pytest
from flask_core import (
    PlatformEvent,
    StageEnvelope,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from penguin_dal import AsyncDB, Field
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


def _mock_dal(
    tables: tuple[str, ...] = ("community_servers", "announcement_broadcasts"),
) -> mock.MagicMock:
    """Build a `MagicMock` standing in for `penguin_dal`'s `AsyncDB`.

    `dal(query)` is sync (it returns an `AsyncQuerySet`), so the DAL
    itself must be a `MagicMock`, not an `AsyncMock`. `tables` is
    pre-populated so `_ensure_announcement_tables`'s idempotent
    `"x" not in dal.tables` guard is a no-op -- without it, a bare
    `MagicMock()` would try to `await` its own auto-created (non-async)
    `.define_table(...)` attribute and raise `TypeError`.
    """
    dal = mock.MagicMock()
    dal.tables = tables
    dal.return_value.select = mock.AsyncMock(return_value=[])
    return dal


class _FakeQuery:
    """Minimal fake `penguin_dal.Query` -- combinable via `&`, carries its own row predicate."""

    def __init__(self, predicate: Any) -> None:
        self._predicate = predicate

    def __and__(self, other: _FakeQuery) -> _FakeQuery:
        return _FakeQuery(lambda row: self._predicate(row) and other._predicate(row))


class _FakeField:
    """Minimal fake `penguin_dal.FieldProxy` -- only `==`/`.belongs()`, this bundle's own surface."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __eq__(self, other: object) -> _FakeQuery:  # type: ignore[override]
        return _FakeQuery(lambda row: getattr(row, self._name, None) == other)

    def belongs(self, values: list[Any]) -> _FakeQuery:
        return _FakeQuery(lambda row: getattr(row, self._name, None) in values)


class _FakeTable:
    """Minimal fake `penguin_dal.TableProxy` -- field access (queries) + `.async_insert()`."""

    def __init__(self, parent: _FakeDal, records: list[Any]) -> None:
        self._parent = parent
        self._records = records

    def __getattr__(self, name: str) -> _FakeField:
        return _FakeField(name)

    async def async_insert(self, **kwargs: object) -> int:
        self._records.append(SimpleNamespace(**kwargs))
        return len(self._records)


class _FakeQuerySet:
    """Minimal fake `penguin_dal.AsyncQuerySet` -- only `.select()`, this bundle's own surface."""

    def __init__(self, records: list[Any], query: _FakeQuery | None) -> None:
        self._records = records
        self._query = query

    async def select(self) -> list[Any]:
        if self._query is None:
            return list(self._records)
        return [r for r in self._records if self._query._predicate(r)]


class _FakeDal:
    """In-memory stand-in for `penguin_dal.AsyncDB` -- implements only this bundle's own surface."""

    def __init__(self) -> None:
        self._servers: list[Any] = []
        self._broadcasts: list[Any] = []
        self.tables = ("community_servers", "announcement_broadcasts")
        self.community_servers = _FakeTable(self, self._servers)
        self.announcement_broadcasts = _FakeTable(self, self._broadcasts)

    def __call__(self, query: _FakeQuery) -> _FakeQuerySet:
        return _FakeQuerySet(self._servers, query)

    def add_server(self, id: int, platform: str, community_id: int = 1) -> None:
        """Add a test server."""
        self._servers.append(SimpleNamespace(id=id, platform=platform, community_id=community_id))

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

    async def test_broadcasts_to_multiple_platforms(self) -> None:
        """Test broadcasting to multiple platforms."""
        mock_dal = _mock_dal()
        mock_discord_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_twitch_server = mock.MagicMock(id=2, platform="twitch", community_id=1)
        mock_dal.return_value.select = mock.AsyncMock(return_value=[
            mock_discord_server,
            mock_twitch_server,
        ])

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

    async def test_records_broadcast_results_in_db(self) -> None:
        """Test that broadcast attempts are recorded in announcement_broadcasts."""
        mock_dal = _mock_dal()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.return_value.select = mock.AsyncMock(return_value=[mock_server])
        mock_dal.announcement_broadcasts.async_insert = mock.AsyncMock()

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
        mock_dal.announcement_broadcasts.async_insert.assert_called()
        call_args = mock_dal.announcement_broadcasts.async_insert.call_args
        assert call_args[1]["announcement_id"] == 42
        assert call_args[1]["community_server_id"] == 1
        assert call_args[1]["platform"] == "discord"
        assert call_args[1]["status"] == "sent"

    async def test_handles_partial_platform_failure(self) -> None:
        """Test when some platforms succeed and others fail."""
        mock_dal = _mock_dal()
        mock_discord_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_twitch_server = mock.MagicMock(id=2, platform="twitch", community_id=1)
        mock_dal.return_value.select = mock.AsyncMock(return_value=[
            mock_discord_server,
            mock_twitch_server,
        ])

        def handler(request: httpx.Request) -> httpx.Response:
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

    async def test_all_failures_is_retryable(self) -> None:
        """Test that all platforms failing is retryable (network issue)."""
        mock_dal = _mock_dal()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.return_value.select = mock.AsyncMock(return_value=[mock_server])

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

    async def test_missing_announcement_data_is_non_retryable(self) -> None:
        """Test that missing announcement dict is non-retryable."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="announcement"):
                await broadcast_announcement(
                    _envelope(announcement="missing"),  # type: ignore
                    _config(),
                    http_client=client,
                )

    async def test_missing_target_platforms_is_non_retryable(self) -> None:
        """Test that missing target_platforms is non-retryable."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="target_platforms"):
                await broadcast_announcement(
                    _envelope(target_platforms=[]),
                    _config(),
                    http_client=client,
                )

    async def test_missing_announcement_id_is_non_retryable(self) -> None:
        """Test that missing announcement_id is non-retryable."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="announcement_id"):
                await broadcast_announcement(
                    _envelope(announcement_id="not_an_int"),  # type: ignore
                    _config(),
                    http_client=client,
                )

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

    async def test_no_servers_found_is_non_retryable(self) -> None:
        """Test that no matching servers is non-retryable."""
        mock_dal = _mock_dal()
        mock_dal.return_value.select = mock.AsyncMock(return_value=[])

        async with _client(lambda r: httpx.Response(200)) as client:
            with mock.patch(
                "bundles.community_announcements_action.get_bundle_dal",
                return_value=mock_dal,
            ):
                with pytest.raises(NonRetryableTransportError, match="no active servers"):
                    await broadcast_announcement(_envelope(), _config(), http_client=client)

    async def test_network_timeout_is_retryable(self) -> None:
        """Test that network timeouts are retryable."""
        mock_dal = _mock_dal()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.return_value.select = mock.AsyncMock(return_value=[mock_server])

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

    async def test_preserves_announcement_data(self) -> None:
        """Test that announcement data is correctly passed to endpoint."""
        mock_dal = _mock_dal()
        mock_server = mock.MagicMock(id=1, platform="discord", community_id=1)
        mock_dal.return_value.select = mock.AsyncMock(return_value=[mock_server])

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

    async def test_missing_platform_endpoint_returns_false(self) -> None:
        """Test that missing platform endpoint is handled gracefully."""
        from bundles.community_announcements_action import _post_to_platform

        client = httpx.AsyncClient()
        result = await _post_to_platform(client, "unknown_platform", {})

        assert result[0] is False
        assert "No action endpoint configured" in result[1]

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
async def real_dal(tmp_path: Path) -> AsyncIterator[AsyncDB]:
    """Real sqlite `penguin_dal.AsyncDB` -- `tenants`/`communities`/`community_servers` created.

    `_ensure_announcement_tables` always binds `community_servers`/
    `announcement_broadcasts` with `migrate=False` (schema owned by
    `000_create_base_schema.sql`, assumed to already exist against real
    Postgres) -- a throwaway sqlite file has no such table until
    something actually creates it, so this fixture defines the identical
    column set first (same two-tier convention `test_bundles_twitch_
    shoutout_action.py`'s own `dal` fixture uses). `_ensure_announcement_
    tables`'s own `if "community_servers" not in dal.tables` guard then
    finds both tables already registered and is a no-op when the bundle
    runs.
    """
    async_dal = AsyncDB(f"sqlite+aiosqlite:///{tmp_path}/announcements_test.db", pool_size=1)
    await async_dal.define_table("tenants", migrate=False)
    await async_dal.define_table(
        "communities", Field("tenant_id", "reference tenants"), migrate=False
    )
    await async_dal.define_table(
        "community_servers",
        Field("community_id", "reference communities", notnull=True),
        Field("platform", "string", notnull=True),
        migrate=False,
    )
    await async_dal.define_table(
        "announcement_broadcasts",
        Field("announcement_id", "integer", notnull=True),
        Field("community_server_id", "integer"),
        Field("platform", "string", notnull=True),
        Field("status", "string", default="pending"),
        Field("error_message", "string"),
        Field("broadcasted_at", "datetime"),
        Field("created_at", "datetime"),
        migrate=False,
    )
    await async_dal.tenants.async_insert()
    await async_dal.communities.async_insert(tenant_id=1)
    set_bundle_dal(async_dal)
    try:
        yield async_dal
    finally:
        reset_bundle_dal_for_tests()
        await async_dal.close()


class TestRealDal:
    """Real-DB smoke (gh-298): exercises the actual `penguin_dal` query path, not a fake/mock."""

    async def test_broadcast_against_real_sqlite_inserts_and_records(
        self, real_dal: AsyncDB
    ) -> None:
        # regression: gh-298
        """Insert one community_server, broadcast, read back the audit row via `penguin_dal`."""
        d = real_dal
        server_id = await d.community_servers.async_insert(community_id=1, platform="discord")

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

        broadcasts = await d(d.announcement_broadcasts.community_server_id == server_id).select()
        assert len(broadcasts) == 1
        assert broadcasts[0].platform == "discord"
        assert broadcasts[0].status == "sent"
        assert broadcasts[0].announcement_id == 42
