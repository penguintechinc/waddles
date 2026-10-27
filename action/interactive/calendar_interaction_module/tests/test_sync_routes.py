"""Tests for app.py's `/sync/enable` and manual-resync routes.

Drives the route coroutines directly inside a Quart `test_request_context`
(not a full `test_client()` round trip) so `before_serving`'s real
`init_services()` -- which opens a real DB connection -- never runs;
`app.calendar_service` is swapped for a mock directly on the module.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app as app_module
from services.event_discord_sync_client import DiscordSyncResult, SyncEnableResult


@pytest.fixture(autouse=True)
def mock_calendar_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.sync_client = AsyncMock()
    monkeypatch.setattr(app_module, 'calendar_service', service)
    return service


class TestEnableSync:
    async def test_defaults_to_enabling(self, mock_calendar_service: AsyncMock) -> None:
        mock_calendar_service.sync_client.set_event_sync_enabled = AsyncMock(
            return_value=SyncEnableResult(
                ok=True, community_id=9, event_sync_enabled=True, pairings_updated=2, error=None
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/sync/enable', method='POST'
        ):
            response = await app_module.enable_sync(9)
        body, status = response
        data = await body.get_json()
        assert status == 200
        assert data['data']['event_sync_enabled'] is True
        assert data['data']['pairings_updated'] == 2
        mock_calendar_service.sync_client.set_event_sync_enabled.assert_awaited_once_with(9, True)

    async def test_explicit_disable(self, mock_calendar_service: AsyncMock) -> None:
        mock_calendar_service.sync_client.set_event_sync_enabled = AsyncMock(
            return_value=SyncEnableResult(
                ok=True, community_id=9, event_sync_enabled=False, pairings_updated=2, error=None
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/sync/enable', method='POST', json={'enabled': False}
        ):
            response = await app_module.enable_sync(9)
        _, status = response
        assert status == 200
        mock_calendar_service.sync_client.set_event_sync_enabled.assert_awaited_once_with(9, False)

    async def test_hub_api_failure_surfaces_502(self, mock_calendar_service: AsyncMock) -> None:
        mock_calendar_service.sync_client.set_event_sync_enabled = AsyncMock(
            return_value=SyncEnableResult(
                ok=False, community_id=9, event_sync_enabled=None, pairings_updated=0,
                error="unreachable"
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/sync/enable', method='POST'
        ):
            response = await app_module.enable_sync(9)
        _, status = response
        assert status == 502


class TestManualSync:
    async def test_event_not_found_404(self, mock_calendar_service: AsyncMock) -> None:
        mock_calendar_service.get_event = AsyncMock(return_value=None)
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/events/5/sync', method='POST'
        ):
            response = await app_module.manual_sync(9, 5)
        _, status = response
        assert status == 404
        mock_calendar_service.sync_client.sync_event.assert_not_awaited()

    async def test_never_pushed_event_uses_create_action(
        self, mock_calendar_service: AsyncMock
    ) -> None:
        mock_calendar_service.get_event = AsyncMock(
            return_value={'status': 'approved', 'sync': {'discord_event_id': None}}
        )
        mock_calendar_service.sync_client.sync_event = AsyncMock(
            return_value=DiscordSyncResult(
                ok=True, event_id=5, discord_event_id='d-1', sync_status='synced', sync_error=None
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/events/5/sync', method='POST'
        ):
            response = await app_module.manual_sync(9, 5)
        body, status = response
        data = await body.get_json()
        assert status == 200
        assert data['data']['action'] == 'create'
        mock_calendar_service.sync_client.sync_event.assert_awaited_once_with(5, 'create')

    async def test_already_pushed_event_uses_update_action(
        self, mock_calendar_service: AsyncMock
    ) -> None:
        mock_calendar_service.get_event = AsyncMock(
            return_value={'status': 'approved', 'sync': {'discord_event_id': 'd-existing'}}
        )
        mock_calendar_service.sync_client.sync_event = AsyncMock(
            return_value=DiscordSyncResult(
                ok=True, event_id=5, discord_event_id='d-existing', sync_status='synced',
                sync_error=None
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/events/5/sync', method='POST'
        ):
            response = await app_module.manual_sync(9, 5)
        body, status = response
        data = await body.get_json()
        assert status == 200
        assert data['data']['action'] == 'update'
        mock_calendar_service.sync_client.sync_event.assert_awaited_once_with(5, 'update')

    async def test_cancelled_event_uses_cancel_action(
        self, mock_calendar_service: AsyncMock
    ) -> None:
        mock_calendar_service.get_event = AsyncMock(
            return_value={'status': 'cancelled', 'sync': {'discord_event_id': 'd-existing'}}
        )
        mock_calendar_service.sync_client.sync_event = AsyncMock(
            return_value=DiscordSyncResult(
                ok=True, event_id=5, discord_event_id=None, sync_status='synced', sync_error=None
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/events/5/sync', method='POST'
        ):
            response = await app_module.manual_sync(9, 5)
        body, status = response
        data = await body.get_json()
        assert status == 200
        assert data['data']['action'] == 'cancel'
        mock_calendar_service.sync_client.sync_event.assert_awaited_once_with(5, 'cancel')

    async def test_hub_api_failure_surfaces_502(self, mock_calendar_service: AsyncMock) -> None:
        mock_calendar_service.get_event = AsyncMock(
            return_value={'status': 'approved', 'sync': {'discord_event_id': None}}
        )
        mock_calendar_service.sync_client.sync_event = AsyncMock(
            return_value=DiscordSyncResult(
                ok=False, event_id=5, discord_event_id=None, sync_status=None,
                sync_error="unreachable"
            )
        )
        async with app_module.app.test_request_context(
            '/api/v1/calendar/9/events/5/sync', method='POST'
        ):
            response = await app_module.manual_sync(9, 5)
        _, status = response
        assert status == 502
