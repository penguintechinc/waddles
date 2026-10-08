"""Tests for CalendarService's Discord event-sync lifecycle triggers.

Mocks `self.dal.execute` (one generic truthy row, enough to satisfy every
`RETURNING`/audit-log statement these methods issue) and `self.sync_client`
(the HTTP client, `services/event_discord_sync_client.py`) so each test
isolates the TRIGGER CONDITION -- which action fires, when, and that
firing never blocks the caller -- without needing a real Postgres row to
drive `get_event()`'s full column mapping. `get_event` itself is
monkeypatched per-test to return the "existing event" shape each trigger
branches on (`status` / `sync.discord_event_id`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.calendar_service import CalendarService
from services.event_discord_sync_client import DiscordSyncResult

USER = {
    'user_id': 1, 'username': 'alice', 'platform': 'discord',
    'platform_user_id': 'u1', 'role': 'admin'
}


def _sync_result(**overrides: Any) -> DiscordSyncResult:
    defaults: dict[str, Any] = {
        'ok': True, 'event_id': 5, 'discord_event_id': 'd-1',
        'sync_status': 'synced', 'sync_error': None,
    }
    defaults.update(overrides)
    return DiscordSyncResult(**defaults)


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        'id': 5,
        'community_id': 10,
        'title': 'Old title',
        'description': 'desc',
        'event_date': '2026-01-01T00:00:00',
        'end_date': None,
        'location': 'loc',
        'status': 'approved',
        'sync': {'discord_event_id': 'd-existing'},
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_dal() -> AsyncMock:
    dal = AsyncMock()
    now = datetime.now(timezone.utc)
    dal.execute = AsyncMock(return_value=[{
        'id': 5, 'event_uuid': 'e2b5c1d0-0000-0000-0000-000000000000',
        'created_at': now, 'updated_at': now, 'approved_at': now,
    }])
    return dal


@pytest.fixture
def sync_client() -> AsyncMock:
    client = AsyncMock()
    client.sync_event = AsyncMock(return_value=_sync_result())
    return client


@pytest.fixture
def service(mock_dal: AsyncMock, sync_client: AsyncMock) -> CalendarService:
    return CalendarService(mock_dal, permission_service=None, sync_client=sync_client)


async def _settle() -> None:
    """Let any `asyncio.create_task`-scheduled fire-and-forget work run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


class TestCreateEventTrigger:
    async def test_auto_approved_triggers_create(
        self, service: CalendarService, sync_client: AsyncMock
    ) -> None:
        data = {'community_id': 10, 'title': 'T', 'event_date': datetime.now(timezone.utc)}
        result = await service.create_event(data, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_awaited_once_with(5, 'create')

    async def test_fire_and_forget_never_blocks_the_caller(
        self, service: CalendarService, sync_client: AsyncMock
    ) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_sync(event_id: int, action: str) -> DiscordSyncResult:
            started.set()
            await release.wait()
            return _sync_result()

        sync_client.sync_event = slow_sync
        data = {'community_id': 10, 'title': 'T', 'event_date': datetime.now(timezone.utc)}

        result = await asyncio.wait_for(service.create_event(data, USER), timeout=1)
        assert result is not None

        await asyncio.sleep(0)
        assert started.is_set(), "background sync task never started"

        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        release.set()
        await asyncio.gather(*pending)

    async def test_sync_client_exception_never_fails_the_request(
        self, service: CalendarService, sync_client: AsyncMock
    ) -> None:
        sync_client.sync_event = AsyncMock(side_effect=RuntimeError("boom"))
        data = {'community_id': 10, 'title': 'T', 'event_date': datetime.now(timezone.utc)}

        result = await service.create_event(data, USER)
        assert result is not None
        await _settle()


class TestApproveEventTrigger:
    async def test_approve_triggers_create(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service, 'get_event', AsyncMock(return_value=_event(status='pending')))
        result = await service.approve_event(5, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_awaited_once_with(5, 'create')


class TestUpdateEventTrigger:
    async def test_discord_visible_change_on_approved_synced_event_triggers_update(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, 'get_event', AsyncMock(return_value=_event(status='approved', title='Old title'))
        )
        result = await service.update_event(5, {'title': 'New title'}, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_awaited_once_with(5, 'update')

    async def test_location_change_also_triggers_update(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, 'get_event', AsyncMock(return_value=_event(status='approved', location='Old loc'))
        )
        result = await service.update_event(5, {'location': 'New loc'}, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_awaited_once_with(5, 'update')

    async def test_non_discord_field_change_does_not_trigger(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service, 'get_event', AsyncMock(return_value=_event(status='approved')))
        result = await service.update_event(5, {'max_attendees': 50}, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_not_awaited()

    async def test_pending_event_with_discord_field_change_does_not_trigger(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, 'get_event', AsyncMock(return_value=_event(status='pending', title='Old title'))
        )
        result = await service.update_event(5, {'title': 'New title'}, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_not_awaited()

    async def test_no_discord_event_id_does_not_trigger(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service,
            'get_event',
            AsyncMock(return_value=_event(
                status='approved', title='Old title', sync={'discord_event_id': None}
            )),
        )
        result = await service.update_event(5, {'title': 'New title'}, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_not_awaited()

    async def test_unchanged_value_does_not_count_as_a_change(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, 'get_event', AsyncMock(return_value=_event(status='approved', title='Same title'))
        )
        result = await service.update_event(5, {'title': 'Same title'}, USER)
        assert result is not None
        await _settle()
        sync_client.sync_event.assert_not_awaited()


class TestDeleteEventTrigger:
    async def test_delete_with_discord_event_id_triggers_cancel(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, 'get_event', AsyncMock(return_value=_event(sync={'discord_event_id': 'd-1'}))
        )
        result = await service.delete_event(5, USER)
        assert result is True
        await _settle()
        sync_client.sync_event.assert_awaited_once_with(5, 'cancel')

    async def test_delete_without_discord_event_id_does_not_trigger(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, 'get_event', AsyncMock(return_value=_event(sync={'discord_event_id': None}))
        )
        result = await service.delete_event(5, USER)
        assert result is True
        await _settle()
        sync_client.sync_event.assert_not_awaited()


class TestRejectEventNeverSyncs:
    async def test_reject_never_calls_sync_client(
        self, service: CalendarService, sync_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service, 'get_event', AsyncMock(return_value=_event(status='pending')))
        result = await service.reject_event(5, 'not relevant', USER)
        assert result is True
        await _settle()
        sync_client.sync_event.assert_not_awaited()
