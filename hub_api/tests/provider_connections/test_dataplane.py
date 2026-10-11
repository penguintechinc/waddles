"""Data-plane flows: which connections to run, and a fresh token for one (refresh under lock)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from services.provider_connections import service as service_module
from services.provider_connections import store
from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.errors import (
    ConnectionNotFound,
    ConnectionNotUsable,
    InvalidConnectionInput,
    ProviderUnavailable,
    UnknownProvider,
)
from services.provider_connections.service import ProviderConnectionService
from services.provider_connections.twitch import TwitchAdapter
from services.provider_connections.vault import PlatformKeyVault
from tests.provider_connections.conftest import (
    BOT,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    OTHER_CHANNEL_LOGIN,
    FakeClock,
    make_registry,
)
from tests.provider_connections.fake_twitch import APP_CLIENT_ID, FakeTwitch
from tests.provider_connections.support import create_twitch, grant_to, raw_rows, set_column


def _ids(pc_db: Any) -> tuple[int, int]:
    return pc_db.seed.community_id, pc_db.seed.tenant_id


class TestListForDataPlane:
    async def test_lists_usable_bindings_across_tenants_without_any_token(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        a = await create_twitch(pc_service, pc_db, bot_tokens)
        access, refresh = fake_twitch.mint(account=BOT)
        b = await create_twitch(
            pc_service,
            pc_db,
            (access, refresh),
            tenant_id=pc_db.seed.other_tenant_id,
            community_id=pc_db.seed.other_community_id,
        )
        page = await pc_service.list_for_dataplane(
            provider="twitch", capability=None, after=0, limit=10
        )
        assert {i.connection_id for i in page.items} == {
            a.binding.connection.id,
            b.binding.connection.id,
        }
        assert page.next_after is None
        item = next(i for i in page.items if i.connection_id == a.binding.connection.id)
        assert (item.community_id, item.tenant_id, item.provider, item.resource_id) == (
            pc_db.seed.community_id,
            pc_db.seed.tenant_id,
            "twitch",
            CHANNEL_ID,
        )
        assert item.metadata == {
            "channel_login": CHANNEL_LOGIN,
            "channel_id": CHANNEL_ID,
            "bot_login": BOT.login,
            "bot_user_id": BOT.user_id,
            "client_id": APP_CLIENT_ID,
            "scopes": ["chat:read", "chat:edit"],
            "capabilities": ["action_relay", "chat_ingest"],
        }
        flat = repr(page)
        assert bot_tokens[0] not in flat and bot_tokens[1] not in flat

    async def test_excludes_disabled_pending_revoked_and_expired(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        usable = await create_twitch(pc_service, pc_db, bot_tokens)
        a2, r2 = fake_twitch.mint(account=BOT)
        disabled = await create_twitch(
            pc_service, pc_db, (a2, r2), channel=OTHER_CHANNEL_LOGIN, enabled=False
        )
        a3, r3 = fake_twitch.mint(account=BOT)
        fake_twitch.channels["third"] = "7003"
        pending = await create_twitch(
            pc_service,
            pc_db,
            (a3, r3),
            community_id=pc_db.seed.second_community_id,
            channel="third",
        )
        await set_column(
            pc_db, "community_connection_access", pending.binding.grant.id, status="pending"
        )
        a4, r4 = fake_twitch.mint(account=BOT)
        fake_twitch.channels["fourth"] = "7004"
        expired = await create_twitch(pc_service, pc_db, (a4, r4), channel="fourth")
        await set_column(
            pc_db, "platform_connections", expired.binding.connection.id, status="expired"
        )
        a5, r5 = fake_twitch.mint(account=BOT)
        fake_twitch.channels["fifth"] = "7005"
        revoked_grant = await create_twitch(pc_service, pc_db, (a5, r5), channel="fifth")
        await set_column(
            pc_db, "community_connection_access", revoked_grant.binding.grant.id, status="revoked"
        )

        page = await pc_service.list_for_dataplane(
            provider="twitch", capability=None, after=0, limit=50
        )
        assert [i.connection_id for i in page.items] == [usable.binding.connection.id]
        assert disabled.binding.connection.id not in {i.connection_id for i in page.items}

    async def test_capability_filter_follows_the_granted_scopes(
        self, pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        read_only, ro_refresh = fake_twitch.mint(account=BOT, scopes=["chat:read"])
        a = await create_twitch(pc_service, pc_db, (read_only, ro_refresh))
        both, both_refresh = fake_twitch.mint(account=BOT)
        b = await create_twitch(
            pc_service, pc_db, (both, both_refresh), channel=OTHER_CHANNEL_LOGIN
        )
        ingest = await pc_service.list_for_dataplane(
            provider="twitch", capability="chat_ingest", after=0, limit=10
        )
        relay = await pc_service.list_for_dataplane(
            provider="twitch", capability="action_relay", after=0, limit=10
        )
        assert {i.connection_id for i in ingest.items} == {
            a.binding.connection.id,
            b.binding.connection.id,
        }
        assert {i.connection_id for i in relay.items} == {b.binding.connection.id}

    async def test_pagination_by_grant_cursor_visits_every_binding_once(
        self, pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        created = []
        for n in range(5):
            login = f"chan{n}x"
            fake_twitch.channels[login] = f"80{n}"
            access, refresh = fake_twitch.mint(account=BOT)
            created.append(await create_twitch(pc_service, pc_db, (access, refresh), channel=login))
        seen: list[int] = []
        after = 0
        pages = 0
        while True:
            page = await pc_service.list_for_dataplane(
                provider="twitch", capability=None, after=after, limit=2
            )
            seen.extend(i.connection_id for i in page.items)
            pages += 1
            if page.next_after is None:
                break
            after = page.next_after
        assert seen == [c.binding.connection.id for c in created]
        assert pages == 3

    async def test_a_filtered_page_can_be_short_without_ending_the_scan(
        self, pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        ro, ro_r = fake_twitch.mint(account=BOT, scopes=["chat:read"])
        await create_twitch(pc_service, pc_db, (ro, ro_r))
        page = await pc_service.list_for_dataplane(
            provider="twitch", capability="action_relay", after=0, limit=1
        )
        assert page.items == [] and page.next_after is not None  # more may follow; keep paging

    async def test_limit_is_clamped(self, pc_service: ProviderConnectionService) -> None:
        page = await pc_service.list_for_dataplane(
            provider="twitch", capability=None, after=0, limit=10**9
        )
        assert page.items == []
        page = await pc_service.list_for_dataplane(
            provider="twitch", capability=None, after=0, limit=-5
        )
        assert page.items == []

    async def test_unknown_provider_and_unknown_capability(
        self, pc_service: ProviderConnectionService
    ) -> None:
        with pytest.raises(UnknownProvider):
            await pc_service.list_for_dataplane(provider="slack", capability=None, after=0, limit=1)
        with pytest.raises(InvalidConnectionInput, match="capability"):
            await pc_service.list_for_dataplane(
                provider="twitch", capability="telepathy", after=0, limit=1
            )


class TestResolve:
    async def test_returns_the_access_token_only_and_the_runtime_metadata(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        resolved = await pc_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert resolved.access_token == bot_tokens[0]
        assert resolved.provider == "twitch" and resolved.community_id == pc_db.seed.community_id
        assert resolved.metadata["bot_login"] == BOT.login
        assert resolved.metadata["channel_login"] == CHANNEL_LOGIN
        assert bot_tokens[0] not in repr(resolved)  # repr-safe
        assert not hasattr(resolved, "refresh_token")

    async def test_no_provider_call_when_the_token_is_comfortably_valid(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        calls = len(fake_twitch.calls)
        await pc_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert len(fake_twitch.calls) == calls

    async def test_a_non_expiring_token_is_never_refreshed(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
    ) -> None:
        access, refresh = fake_twitch.mint(account=BOT, expires_in=0)
        view = await create_twitch(pc_service, pc_db, (access, refresh))
        clock.advance(hours=24 * 365)
        resolved = await pc_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert resolved.access_token == access and fake_twitch.refresh_calls == 0

    async def test_a_token_inside_the_skew_window_is_refreshed_and_rotation_is_persisted(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        clock.advance(seconds=14000 - 60)  # 60 s left, inside the 120 s skew
        resolved = await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert fake_twitch.refresh_calls == 1
        assert resolved.access_token != bot_tokens[0]
        assert resolved.expires_at == clock.now + timedelta(seconds=14400)

        row = (await raw_rows(pc_db, "platform_connections"))[0]
        vault = PlatformKeyVault()
        assert vault.open(row["access_token"]) == resolved.access_token
        new_refresh = vault.open(row["refresh_token"])
        assert new_refresh and new_refresh != bot_tokens[1]  # the rotated pair replaced the old

        # A second resolve reuses the stored token: still exactly one refresh.
        again = await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert again.access_token == resolved.access_token and fake_twitch.refresh_calls == 1

    async def test_a_refresh_that_keeps_the_old_refresh_token_when_the_provider_omits_one(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        before = (await raw_rows(pc_db, "platform_connections"))[0]["refresh_token"]
        fake_twitch.raw_body_override["token"] = (
            b'{"access_token": "freshAccessTokenZ", "expires_in": 3600, "scope": ["chat:read"]}'
        )
        clock.advance(seconds=14000)
        resolved = await pc_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert resolved.access_token == "freshAccessTokenZ"
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert row["refresh_token"] == before  # untouched ciphertext, still the working chain

    async def test_a_rejected_refresh_marks_the_link_expired_and_keeps_it_for_reconnect(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        fake_twitch.refresh_tokens.clear()  # Twitch no longer honours the refresh token
        clock.advance(seconds=14000)
        with pytest.raises(ConnectionNotUsable) as info:
            await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert info.value.reason == "refresh_rejected"

        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert (row["status"], row["status_reason"]) == ("expired", "refresh_rejected")
        # The data plane now sees it as unusable, and the admin sees why.
        with pytest.raises(ConnectionNotUsable) as again:
            await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert again.value.reason == "refresh_rejected"
        listed = await pc_service.list_for_dataplane(
            provider="twitch", capability=None, after=0, limit=10
        )
        assert listed.items == []
        shown = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id, connection_id=cid
        )
        assert shown.binding.connection.status == "expired" and not shown.usable

        # Reconnecting with fresh credentials revives the same connection and clears the reason.
        new_access, new_refresh = fake_twitch.mint(account=BOT)
        await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=cid,
            enabled=None,
            metadata=None,
            access_token=new_access,
            refresh_token=new_refresh,
        )
        revived = await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert revived.access_token == new_access

    async def test_a_transient_provider_failure_changes_nothing(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        before = await raw_rows(pc_db, "platform_connections")
        clock.advance(seconds=14000)
        fake_twitch.status_override["token"] = 503
        with pytest.raises(ProviderUnavailable):
            await pc_service.resolve(
                community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
            )
        assert await raw_rows(pc_db, "platform_connections") == before  # still active, same tokens
        fake_twitch.status_override.clear()
        ok = await pc_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert ok.access_token != bot_tokens[0]  # and it recovers on the next attempt

    async def test_an_unconfigured_platform_app_is_not_a_rejection(
        self, pc_db: Any, bot_tokens: Any, fake_twitch: FakeTwitch, clock: FakeClock
    ) -> None:
        from services.oauth_providers import ProviderNotConfigured

        def unset() -> tuple[str, str]:
            raise ProviderNotConfigured("twitch: missing env")

        registry = AdapterRegistry(
            [TwitchAdapter(transport=fake_twitch.transport, app_credentials=unset, clock=clock)]
        )
        service = ProviderConnectionService(
            pc_db.install_dal, registry, PlatformKeyVault(), clock=clock
        )
        view = await create_twitch(service, pc_db, bot_tokens)
        before = await raw_rows(pc_db, "platform_connections")
        clock.advance(seconds=14000)
        with pytest.raises(ConnectionNotUsable) as info:
            await service.resolve(
                community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
            )
        assert info.value.reason == "refresh_not_configured"
        assert await raw_rows(pc_db, "platform_connections") == before  # an operator fix revives it
        assert fake_twitch.refresh_calls == 0

    async def test_an_expired_token_without_a_refresh_token_marks_the_link_expired(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, with_refresh=False)
        view = await create_twitch(pc_service, pc_db, (access, None))
        clock.advance(seconds=14000)
        with pytest.raises(ConnectionNotUsable) as info:
            await pc_service.resolve(
                community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
            )
        assert info.value.reason == "token_expired"
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert (row["status"], row["status_reason"]) == ("expired", "token_expired")

    async def test_a_worker_that_lost_the_race_uses_the_already_rotated_token(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lock-free read is stale; under the lock the row is already fresh -> no refresh spent."""
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        stale = await store.read_resolve_state(
            pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=cid
        )
        assert stale is not None
        clock.advance(seconds=14000)
        winner_access = PlatformKeyVault().seal("winnerAccessToken")
        fresh_expiry = clock.now + timedelta(hours=4)
        await set_column(
            pc_db, "platform_connections", cid, access_token=winner_access, expires_at=fresh_expiry
        )

        async def fake_read(*args: Any, **kwargs: Any) -> Any:
            return stale

        monkeypatch.setattr(service_module.store, "read_resolve_state", fake_read)
        resolved = await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert resolved.access_token == "winnerAccessToken"
        assert fake_twitch.refresh_calls == 0

    @pytest.mark.parametrize(
        ("mutation", "reason"),
        [
            (("community_connection_access", {"enabled": False}), "disabled"),
            (("community_connection_access", {"status": "pending"}), "pending_approval"),
            (("community_connection_access", {"status": "revoked"}), "revoked_grant"),
            (
                (
                    "platform_connections",
                    {"status": "expired", "status_reason": "refresh_rejected"},
                ),
                "refresh_rejected",
            ),
            (("platform_connections", {"status": "expired"}), "connection_expired"),
            (("platform_connections", {"status": "revoked"}), "connection_revoked"),
        ],
    )
    async def test_every_unusable_state_has_a_precise_reason(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        mutation: tuple[str, dict[str, Any]],
        reason: str,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        table, values = mutation
        row_id = (
            view.binding.grant.id
            if table == "community_connection_access"
            else view.binding.connection.id
        )
        await set_column(pc_db, table, row_id, **values)
        with pytest.raises(ConnectionNotUsable) as info:
            await pc_service.resolve(
                community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
            )
        assert info.value.reason == reason

    async def test_resolve_is_per_community_a_sibling_without_a_grant_gets_nothing(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        with pytest.raises(ConnectionNotFound):
            await pc_service.resolve(
                community_id=pc_db.seed.second_community_id,
                connection_id=view.binding.connection.id,
            )
        await grant_to(
            pc_db,
            community_id=pc_db.seed.second_community_id,
            connection_id=view.binding.connection.id,
        )
        shared = await pc_service.resolve(
            community_id=pc_db.seed.second_community_id, connection_id=view.binding.connection.id
        )
        assert shared.community_id == pc_db.seed.second_community_id

    async def test_unknown_pair(self, pc_service: ProviderConnectionService, pc_db: Any) -> None:
        with pytest.raises(ConnectionNotFound):
            await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=12345)

    async def test_a_provider_with_no_adapter_cannot_be_resolved(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any, clock: FakeClock
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        orphaned = ProviderConnectionService(
            pc_db.install_dal, AdapterRegistry(), PlatformKeyVault(), clock=clock
        )
        with pytest.raises(UnknownProvider):
            await orphaned.resolve(
                community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
            )

    async def test_the_row_vanishing_under_the_lock_is_not_found(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        stale = await store.read_resolve_state(
            pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=cid
        )
        await pc_service.delete(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id, connection_id=cid
        )
        clock.advance(seconds=14000)

        async def fake_read(*args: Any, **kwargs: Any) -> Any:
            return stale

        monkeypatch.setattr(service_module.store, "read_resolve_state", fake_read)
        with pytest.raises(ConnectionNotFound):
            await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)

    async def test_the_registry_wiring_helper_builds_a_working_service(
        self, pc_db: Any, fake_twitch: FakeTwitch, clock: FakeClock, bot_tokens: Any
    ) -> None:
        service = ProviderConnectionService(
            pc_db.install_dal, make_registry(fake_twitch, clock), PlatformKeyVault(), clock=clock
        )
        assert [spec.provider for spec in service.providers()] == ["twitch"]
