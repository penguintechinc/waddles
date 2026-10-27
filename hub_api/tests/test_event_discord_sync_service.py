"""`services/event_discord_sync_service.py` -- Discord event-sync push engine tests.

Discord HTTP calls are mocked via the module's own `DiscordEventTargetClient`
Protocol (fake below) -- no real network access, no `httpx.AsyncClient`
constructed. DB state uses `event_sync_db` (real `bind_bar_citizen_tables()`/
`bind_calendar_sync_tables()`/`bind_auth_tables()` sqlite), same fixture
layering `test_role_sync_service.py` uses for its own group.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import event_discord_sync_service as svc
from services.credential_resolver import PlatformCredentials, TransportUnavailable
from services.guild_pairing import create_pairing, update_pairing

_DISCORD_GUILD_ID = "123456789012345678"  # gitleaks:allow - fake snowflake, not a secret
_DISCORD_GUILD_ID_2 = "223456789012345678"  # gitleaks:allow - fake snowflake, not a secret
_DISCORD_EVENT_ID = "900000000000000001"  # gitleaks:allow - fake snowflake, not a secret
_DISCORD_EVENT_ID_2 = "900000000000000002"  # gitleaks:allow - fake snowflake, not a secret


class _FakeDiscordClient:
    """In-memory create/patch/cancel store, standing in for live Discord REST calls."""

    def __init__(
        self,
        *,
        next_create_id: str = _DISCORD_EVENT_ID,
        patch_404_ids: set[str] | None = None,
        cancel_404_ids: set[str] | None = None,
        raise_on_create: Exception | None = None,
    ) -> None:
        self.next_create_id = next_create_id
        self.patch_404_ids = patch_404_ids or set()
        self.cancel_404_ids = cancel_404_ids or set()
        self.raise_on_create = raise_on_create
        self.created: list[tuple[str, svc.DiscordEventPayload]] = []
        self.patched: list[tuple[str, str, svc.DiscordEventPayload]] = []
        self.cancelled: list[tuple[str, str]] = []

    async def create_scheduled_event(
        self, *, guild_id: str, payload: svc.DiscordEventPayload
    ) -> str:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self.created.append((guild_id, payload))
        return self.next_create_id

    async def patch_scheduled_event(
        self, *, guild_id: str, discord_event_id: str, payload: svc.DiscordEventPayload
    ) -> None:
        if discord_event_id in self.patch_404_ids:
            raise svc.DiscordEventNotFoundError("gone")
        self.patched.append((guild_id, discord_event_id, payload))

    async def cancel_scheduled_event(self, *, guild_id: str, discord_event_id: str) -> None:
        if discord_event_id in self.cancel_404_ids:
            raise svc.DiscordEventNotFoundError("gone")
        self.cancelled.append((guild_id, discord_event_id))


class _FakeCredentialResolver:
    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        return PlatformCredentials(
            tenant_id=tenant_id,
            platform=platform,
            payload={"bot_token": "discord-bot-token"},  # noqa: S105 - test fixture, not a secret
            source="tenant",
        )


class _FailingCredentialResolver:
    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> Any:
        raise TransportUnavailable(f"no creds for {platform}")


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the PostHog flag ON for every test; individual tests override to OFF."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


def _make_pairing(
    dal: Any,
    community_id: int,
    *,
    guild_id: str = _DISCORD_GUILD_ID,
    event_sync_enabled: bool = True,
) -> Any:
    created = create_pairing(
        dal,
        community_id,
        discord_guild_id=guild_id,
        direction="bidirectional",
        role_name_prefix="[ES]",
        actor_user_id=None,
    )
    if event_sync_enabled:
        update_pairing(dal, community_id, created.id, event_sync_enabled=True)
    return dal(dal.guild_tenant_pairings.id == created.id).select().first()


def _targets(client: _FakeDiscordClient, resolver: Any = None) -> list[svc.PlatformEventTarget]:
    return [
        svc.DiscordScheduledEventTarget(
            credential_resolver=resolver or _FakeCredentialResolver(),
            make_client=lambda token: client,
        )
    ]


class TestNoEnabledPairings:
    async def test_no_pairing_is_noop_pending(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient()

        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))

        assert result.sync_status == "pending"
        assert result.sync_error is None
        assert client.created == []

    async def test_feature_flag_off_skips_event(
        self, event_sync_db: Any, _flag_on: AsyncMock
    ) -> None:
        _flag_on.return_value = False
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient()

        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))

        assert result.sync_error is None
        assert client.created == []


class TestCreate:
    async def test_create_pushes_full_payload_and_persists_aggregate(
        self, event_sync_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))

        assert result.sync_status == "synced"
        assert result.discord_event_id == _DISCORD_EVENT_ID
        assert len(client.created) == 1
        guild_id, payload = client.created[0]
        assert guild_id == _DISCORD_GUILD_ID
        assert payload.name == "Test Event"

        updated = dal(dal.calendar_events.id == event_id).select().first()
        assert updated.discord_event_id == _DISCORD_EVENT_ID
        assert updated.sync_status == "synced"
        assert updated.sync_error is None

        sync_row = dal(dal.calendar_event_discord_syncs.event_id == event_id).select().first()
        assert sync_row.discord_event_id == _DISCORD_EVENT_ID
        assert sync_row.sync_status == "synced"

    async def test_fail_closed_credential_resolver_never_raises(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient()

        result = await svc.sync_event(
            dal, event_row, action="create", targets=_targets(client, _FailingCredentialResolver())
        )

        assert result.sync_status == "sync_error"
        assert result.sync_error is not None
        updated = dal(dal.calendar_events.id == event_id).select().first()
        assert updated.sync_status == "sync_error"

    async def test_unexpected_target_exception_never_raises(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        class _BoomTarget:
            platform = "discord"

            async def push(self, dal: Any, event_row: Any, *, action: Any) -> Any:
                raise RuntimeError("boom")

        result = await svc.sync_event(dal, event_row, action="create", targets=[_BoomTarget()])

        assert result.sync_status == "sync_error"
        assert result.sync_error == "unexpected_error"


class TestUpdate:
    async def test_update_with_no_existing_id_creates(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        result = await svc.sync_event(dal, event_row, action="update", targets=_targets(client))

        assert result.sync_status == "synced"
        assert len(client.created) == 1
        assert client.patched == []

    async def test_update_with_existing_id_patches_full_payload(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        await svc.sync_event(dal, event_row, action="create", targets=_targets(client))
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        result = await svc.sync_event(dal, event_row, action="update", targets=_targets(client))

        assert result.sync_status == "synced"
        assert len(client.patched) == 1
        guild_id, discord_event_id, _payload = client.patched[0]
        assert guild_id == _DISCORD_GUILD_ID
        assert discord_event_id == _DISCORD_EVENT_ID

    async def test_404_during_patch_resets_to_pending_drift(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        await svc.sync_event(dal, event_row, action="create", targets=_targets(client))
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        client.patch_404_ids = {_DISCORD_EVENT_ID}
        result = await svc.sync_event(dal, event_row, action="update", targets=_targets(client))

        assert result.sync_status == "pending"
        assert result.discord_event_id is None

        sync_row = dal(dal.calendar_event_discord_syncs.event_id == event_id).select().first()
        assert sync_row.discord_event_id is None
        assert sync_row.sync_status == "pending"


class TestCancel:
    async def test_cancel_sends_status_canceled_not_delete(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        await svc.sync_event(dal, event_row, action="create", targets=_targets(client))
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        result = await svc.sync_event(dal, event_row, action="cancel", targets=_targets(client))

        assert result.sync_status == "synced"
        assert client.cancelled == [(_DISCORD_GUILD_ID, _DISCORD_EVENT_ID)]

    async def test_cancel_with_nothing_to_cancel_is_noop(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient()

        result = await svc.sync_event(dal, event_row, action="cancel", targets=_targets(client))

        assert result.sync_status == "pending"
        assert client.cancelled == []

    async def test_404_during_cancel_resets_to_pending_drift(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        await svc.sync_event(dal, event_row, action="create", targets=_targets(client))
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        client.cancel_404_ids = {_DISCORD_EVENT_ID}
        result = await svc.sync_event(dal, event_row, action="cancel", targets=_targets(client))

        assert result.sync_status == "pending"
        sync_row = dal(dal.calendar_event_discord_syncs.event_id == event_id).select().first()
        assert sync_row.discord_event_id is None


class TestMultiGuild:
    async def test_pushes_to_every_enabled_pairing_independently(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id, guild_id=_DISCORD_GUILD_ID)
        _make_pairing(dal, community_id, guild_id=_DISCORD_GUILD_ID_2)
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        class _SequencedClient(_FakeDiscordClient):
            async def create_scheduled_event(
                self, *, guild_id: str, payload: svc.DiscordEventPayload
            ) -> str:
                event_id_for_guild = (
                    _DISCORD_EVENT_ID if guild_id == _DISCORD_GUILD_ID else _DISCORD_EVENT_ID_2
                )
                self.created.append((guild_id, payload))
                return event_id_for_guild

        client = _SequencedClient()
        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))

        assert result.sync_status == "synced"
        assert len(client.created) == 2
        rows = dal(dal.calendar_event_discord_syncs.event_id == event_id).select()
        assert {r.discord_guild_id for r in rows} == {_DISCORD_GUILD_ID, _DISCORD_GUILD_ID_2}
        assert {r.discord_event_id for r in rows} == {_DISCORD_EVENT_ID, _DISCORD_EVENT_ID_2}

    async def test_one_guild_failing_never_blocks_the_other(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id, guild_id=_DISCORD_GUILD_ID)
        _make_pairing(dal, community_id, guild_id=_DISCORD_GUILD_ID_2)
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        class _OneFailsClient(_FakeDiscordClient):
            async def create_scheduled_event(
                self, *, guild_id: str, payload: svc.DiscordEventPayload
            ) -> str:
                if guild_id == _DISCORD_GUILD_ID:
                    raise svc.DiscordEventSyncError("rate limited")
                self.created.append((guild_id, payload))
                return _DISCORD_EVENT_ID_2

        client = _OneFailsClient()
        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))

        assert result.sync_status == "sync_error"  # any failure -> aggregate sync_error
        rows = {
            r.discord_guild_id: r
            for r in dal(dal.calendar_event_discord_syncs.event_id == event_id).select()
        }
        assert rows[_DISCORD_GUILD_ID].sync_status == "sync_error"
        assert rows[_DISCORD_GUILD_ID_2].sync_status == "synced"
        assert rows[_DISCORD_GUILD_ID_2].discord_event_id == _DISCORD_EVENT_ID_2


class TestReconcileBatch:
    async def test_reconcile_syncs_pending_approved_events(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        client = _FakeDiscordClient(next_create_id=_DISCORD_EVENT_ID)

        async def _make_client(token: str) -> _FakeDiscordClient:
            return client

        summary = await svc.run_event_sync_reconcile_batch(
            dal,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: client,
        )

        assert summary.events_examined == 1
        assert summary.events_synced == 1
        assert summary.events_failed == 0
        updated = dal(dal.calendar_events.id == event_id).select().first()
        assert updated.sync_status == "synced"

    async def test_reconcile_ignores_non_approved_events(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        dal(dal.calendar_events.id == event_id).update(status="pending")
        dal.commit()
        _make_pairing(dal, community_id)
        client = _FakeDiscordClient()

        summary = await svc.run_event_sync_reconcile_batch(
            dal,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: client,
        )

        assert summary.events_examined == 0

    async def test_reconcile_ignores_already_synced_events(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        dal(dal.calendar_events.id == event_id).update(sync_status="synced")
        dal.commit()
        _make_pairing(dal, community_id)
        client = _FakeDiscordClient()

        summary = await svc.run_event_sync_reconcile_batch(
            dal,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: client,
        )

        assert summary.events_examined == 0

    async def test_reconcile_backs_off_after_failure(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)

        class _AlwaysFailsClient(_FakeDiscordClient):
            async def create_scheduled_event(
                self, *, guild_id: str, payload: svc.DiscordEventPayload
            ) -> str:
                raise svc.DiscordEventSyncError("rate limited")

        sleep_calls: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        summary = await svc.run_event_sync_reconcile_batch(
            dal,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: _AlwaysFailsClient(),
            sleep_fn=_fake_sleep,
        )

        assert summary.events_failed == 1
        assert sleep_calls == [svc._BACKOFF_BASE_S * 2]


class TestTenantIsolation:
    async def test_community_with_no_tenant_fails_closed(self, event_sync_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        dal(dal.communities.id == community_id).update(tenant_id=999999)
        dal.commit()
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        client = _FakeDiscordClient()

        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))

        assert result.sync_status == "sync_error"
        assert client.created == []


class TestPayloadBuilder:
    def test_build_discord_payload_defaults_end_and_location(self, event_sync_db: Any) -> None:
        dal, _community_id, _tenant_id, _global_id, event_id = event_sync_db
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        payload = svc.build_discord_payload(event_row)

        assert payload.name == "Test Event"
        assert payload.location == "Online"
        assert payload.scheduled_end_time != ""
        body = payload.as_json()
        assert body["entity_type"] == svc._ENTITY_TYPE_EXTERNAL
        assert body["privacy_level"] == svc._PRIVACY_LEVEL_GUILD_ONLY
        assert body["entity_metadata"]["location"] == "Online"
        assert body["description"] == "A test event"  # fixture seeds a truthy description

    def test_as_json_omits_description_when_falsy(self) -> None:
        payload = svc.DiscordEventPayload(
            name="Test",
            description=None,
            scheduled_start_time="2026-01-01T00:00:00+00:00",
            scheduled_end_time="2026-01-01T01:00:00+00:00",
            location="Online",
        )

        body = payload.as_json()

        assert "description" not in body  # falsy description omitted, not sent as null


class TestHttpDiscordEventTargetClient:
    """Exercises the real `httpx.AsyncClient` call path via `httpx.MockTransport` -- no real network."""  # noqa: E501

    def _payload(self) -> svc.DiscordEventPayload:
        return svc.DiscordEventPayload(
            name="Test",
            description="desc",
            scheduled_start_time="2026-01-01T00:00:00+00:00",
            scheduled_end_time="2026-01-01T01:00:00+00:00",
            location="Online",
        )

    async def test_create_success_returns_discord_event_id(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path.endswith("/scheduled-events")
            return httpx.Response(200, json={"id": _DISCORD_EVENT_ID})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = svc.HttpDiscordEventTargetClient(http_client, bot_token="tok")  # noqa: S106
            result = await client.create_scheduled_event(
                guild_id=_DISCORD_GUILD_ID, payload=self._payload()
            )

        assert result == _DISCORD_EVENT_ID

    async def test_patch_success(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PATCH"
            return httpx.Response(200, json={"id": _DISCORD_EVENT_ID})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = svc.HttpDiscordEventTargetClient(http_client, bot_token="tok")  # noqa: S106
            await client.patch_scheduled_event(
                guild_id=_DISCORD_GUILD_ID,
                discord_event_id=_DISCORD_EVENT_ID,
                payload=self._payload(),
            )

    async def test_patch_404_raises_not_found(self) -> None:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ) as http_client:
            client = svc.HttpDiscordEventTargetClient(http_client, bot_token="tok")  # noqa: S106
            with pytest.raises(svc.DiscordEventNotFoundError):
                await client.patch_scheduled_event(
                    guild_id=_DISCORD_GUILD_ID,
                    discord_event_id=_DISCORD_EVENT_ID,
                    payload=self._payload(),
                )

    async def test_cancel_sends_status_body_never_delete(self) -> None:
        import httpx

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            import json as _json

            captured["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"id": _DISCORD_EVENT_ID})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = svc.HttpDiscordEventTargetClient(http_client, bot_token="tok")  # noqa: S106
            await client.cancel_scheduled_event(
                guild_id=_DISCORD_GUILD_ID, discord_event_id=_DISCORD_EVENT_ID
            )

        assert captured["method"] == "PATCH"
        assert captured["method"] != "DELETE"
        assert captured["body"] == {"status": svc._STATUS_CANCELED}

    @pytest.mark.parametrize(
        ("status", "expected_exc"),
        [
            (401, svc.DiscordEventSyncError),
            (403, svc.DiscordEventSyncError),
            (429, svc.DiscordEventSyncError),
            (500, svc.DiscordEventSyncError),
        ],
    )
    async def test_error_status_codes_classified(self, status: int, expected_exc: type) -> None:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(status))
        ) as http_client:
            client = svc.HttpDiscordEventTargetClient(http_client, bot_token="tok")  # noqa: S106
            with pytest.raises(expected_exc):
                await client.create_scheduled_event(
                    guild_id=_DISCORD_GUILD_ID, payload=self._payload()
                )

    async def test_network_error_wrapped(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = svc.HttpDiscordEventTargetClient(http_client, bot_token="tok")  # noqa: S106
            with pytest.raises(svc.DiscordEventSyncError):
                await client.create_scheduled_event(
                    guild_id=_DISCORD_GUILD_ID, payload=self._payload()
                )


class TestDefaultTargetsAndFailClosedBackstop:
    async def test_default_targets_returns_discord_target(self) -> None:
        import httpx

        async with httpx.AsyncClient() as http_client:
            targets = svc._default_targets(http_client)
            assert len(targets) == 1
            assert targets[0].platform == "discord"

    async def test_sync_event_owns_and_closes_default_http_client(self, event_sync_db: Any) -> None:
        """No `targets=` injected -- `sync_event` must create AND close its own client."""
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        # No enabled pairing -- DiscordScheduledEventTarget.push() returns early
        # (pending/no-op) without ever using the client, exercising the owns/close
        # path without a real network call.
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        result = await svc.sync_event(dal, event_row, action="create")

        assert result.sync_status == "pending"

    async def test_sync_event_outer_backstop_never_raises(self, event_sync_db: Any) -> None:
        import types

        dal, _community_id, _tenant_id, _global_id, event_id = event_sync_db
        bogus_row = types.SimpleNamespace(id=event_id)  # no community_id -- forces AttributeError

        result = await svc.sync_event(dal, bogus_row, action="create", targets=[])

        assert result.sync_status == "sync_error"
        assert result.sync_error == "unexpected_error"

    async def test_target_level_transport_unavailable_paths(self, event_sync_db: Any) -> None:
        """Directly exercises `DiscordScheduledEventTarget.push()`'s own tenant/bot-token guards."""
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        class _EmptyPayloadResolver:
            async def resolve(
                self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
            ) -> Any:
                return PlatformCredentials(
                    tenant_id=tenant_id, platform=platform, payload={}, source="tenant"
                )

        target = svc.DiscordScheduledEventTarget(
            credential_resolver=_EmptyPayloadResolver(),
            make_client=lambda token: _FakeDiscordClient(),
        )
        with pytest.raises(TransportUnavailable):
            await target.push(dal, event_row, action="create")

    async def test_target_level_unexpected_exception_counts_as_failed(
        self, event_sync_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        class _BoomClient(_FakeDiscordClient):
            async def create_scheduled_event(self, *, guild_id: str, payload: Any) -> str:
                raise RuntimeError("kaboom")

        target = svc.DiscordScheduledEventTarget(
            credential_resolver=_FakeCredentialResolver(), make_client=lambda token: _BoomClient()
        )
        result = await target.push(dal, event_row, action="create")

        assert result.sync_status == "sync_error"
        assert result.targets_failed == 1
