"""Connection lifecycle through the real service, adapter, vault and store (sqlite, fake socket)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    CredentialsRejected,
    InsufficientScopes,
    InvalidConnectionInput,
    ProviderUnavailable,
    ResourceUnresolvable,
    UnknownProvider,
)
from services.provider_connections.service import ProviderConnectionService
from services.provider_connections.vault import PlatformKeyVault
from tests.provider_connections.conftest import (
    BOT,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    OTHER_CHANNEL_ID,
    OTHER_CHANNEL_LOGIN,
    FakeClock,
)
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import create_twitch, grant_to, open_row, raw_rows


class TestCreate:
    async def test_stores_a_verified_connection_with_derived_facts(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any, clock: FakeClock
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        conn, grant = view.binding.connection, view.binding.grant
        assert (conn.platform, conn.resource_type, conn.resource_id) == (
            "twitch",
            "twitch_channel",
            CHANNEL_ID,
        )
        assert conn.tenant_id == pc_db.seed.tenant_id
        assert conn.status == "active" and conn.status_reason is None
        assert conn.has_refresh_token
        assert conn.expires_at == clock.now + timedelta(seconds=14000)
        assert conn.installed_by_user_id == pc_db.seed.admin_user_id
        assert (grant.community_id, grant.status, grant.enabled) == (
            pc_db.seed.community_id,
            "approved",
            True,
        )
        assert view.binding.shared_with == 0
        assert view.usable
        assert view.capabilities == ["action_relay", "chat_ingest"]
        assert view.public_metadata == {
            "channel_login": CHANNEL_LOGIN,
            "channel_id": CHANNEL_ID,
            "bot_login": BOT.login,
            "bot_user_id": BOT.user_id,
            "scopes": ["chat:read", "chat:edit"],
            "capabilities": ["action_relay", "chat_ingest"],
        }
        assert "client_id" not in view.public_metadata  # runtime-only key, not shown to admins

    async def test_channel_input_is_normalised(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens, channel="#PenguinzPlays")
        assert view.public_metadata["channel_login"] == CHANNEL_LOGIN

    async def test_irc_style_oauth_prefix_is_accepted_and_stripped_before_storage(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        access, refresh = bot_tokens
        await create_twitch(pc_service, pc_db, (f"oauth:{access}", refresh))
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert open_row(row) == access

    async def test_can_be_created_disabled(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens, enabled=False)
        assert view.binding.grant.enabled is False
        assert not view.usable

    async def test_without_a_refresh_token_it_is_recorded_and_flagged(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        fake_twitch: FakeTwitch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, with_refresh=False)
        with caplog.at_level("INFO"):
            view = await create_twitch(pc_service, pc_db, (access, None))
        assert view.binding.connection.has_refresh_token is False
        assert "created_without_refresh" in caplog.text

    async def test_non_expiring_token_has_no_expiry(
        self, pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        access, refresh = fake_twitch.mint(account=BOT, expires_in=0)
        view = await create_twitch(pc_service, pc_db, (access, refresh))
        assert view.binding.connection.expires_at is None

    @pytest.mark.parametrize(
        ("overrides", "error"),
        [
            ({"provider": "slack"}, UnknownProvider),
            ({"metadata": {}}, InvalidConnectionInput),
            ({"metadata": {"channel_login": "x"}}, InvalidConnectionInput),
            ({"metadata": {"channel_login": CHANNEL_LOGIN, "evil": "1"}}, InvalidConnectionInput),
            ({"access_token": "has space"}, InvalidConnectionInput),
            ({"access_token": ""}, InvalidConnectionInput),
            ({"access_token": "oauth:"}, InvalidConnectionInput),
            ({"access_token": "no-such-token"}, CredentialsRejected),
            ({"metadata": {"channel_login": "nosuchchannel"}}, ResourceUnresolvable),
        ],
        ids=[
            "unknown-provider",
            "missing-channel",
            "short-channel",
            "unknown-metadata-key",
            "token-with-space",
            "empty-token",
            "prefix-only-token",
            "rejected-token",
            "unknown-channel",
        ],
    )
    async def test_invalid_input_stores_nothing(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        overrides: dict[str, Any],
        error: type[Exception],
    ) -> None:
        with pytest.raises(error):
            await create_twitch(pc_service, pc_db, bot_tokens, **overrides)
        assert await raw_rows(pc_db, "platform_connections") == []
        assert await raw_rows(pc_db, "community_connection_access") == []

    async def test_missing_scope_stores_nothing(
        self, pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        access, refresh = fake_twitch.mint(account=BOT, scopes=["user:read:email"])
        with pytest.raises(InsufficientScopes):
            await create_twitch(pc_service, pc_db, (access, refresh))
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_provider_outage_stores_nothing(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        fake_twitch.down = True
        with pytest.raises(ProviderUnavailable):
            await create_twitch(pc_service, pc_db, bot_tokens)
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_a_refresh_token_for_a_non_refreshable_provider_is_refused(
        self, pc_db: Any, fake_twitch: FakeTwitch, clock: FakeClock
    ) -> None:
        from tests.provider_connections.fake_provider import make_fake_registry

        service = ProviderConnectionService(
            pc_db.install_dal, make_fake_registry(), PlatformKeyVault(), clock=clock
        )
        with pytest.raises(InvalidConnectionInput, match="does not use a refresh token"):
            await service.create(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=None,
                provider="fakechat",
                access_token="tok1",
                refresh_token="ref1",
                metadata={"room": "lobby"},
                enabled=True,
            )


class TestConflicts:
    async def test_the_same_channel_cannot_be_connected_twice_and_tokens_are_not_overwritten(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        first = await create_twitch(pc_service, pc_db, bot_tokens)
        before = await raw_rows(pc_db, "platform_connections")
        other_access, other_refresh = fake_twitch.mint(account=BOT)
        with pytest.raises(ConnectionConflict, match="already connected"):
            await create_twitch(
                pc_service,
                pc_db,
                (other_access, other_refresh),
                community_id=pc_db.seed.second_community_id,
            )
        after = await raw_rows(pc_db, "platform_connections")
        assert after == before  # not one byte of the existing link changed
        assert open_row(after[0]) == bot_tokens[0]
        assert len(await raw_rows(pc_db, "community_connection_access")) == 1
        assert first.binding.connection.id == after[0]["id"]

    async def test_the_same_resource_in_another_tenant_is_independent(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        await create_twitch(pc_service, pc_db, bot_tokens)
        access, refresh = fake_twitch.mint(account=BOT)
        other = await create_twitch(
            pc_service,
            pc_db,
            (access, refresh),
            tenant_id=pc_db.seed.other_tenant_id,
            community_id=pc_db.seed.other_community_id,
        )
        assert other.binding.connection.tenant_id == pc_db.seed.other_tenant_id
        assert len(await raw_rows(pc_db, "platform_connections")) == 2


class TestReadAndList:
    async def test_list_get_roundtrip(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        a = await create_twitch(pc_service, pc_db, bot_tokens)
        access, refresh = fake_twitch.mint(account=BOT)
        b = await create_twitch(pc_service, pc_db, (access, refresh), channel=OTHER_CHANNEL_LOGIN)
        listed = await pc_service.list(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id
        )
        assert [v.binding.connection.id for v in listed] == [
            a.binding.connection.id,
            b.binding.connection.id,
        ]
        got = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            connection_id=b.binding.connection.id,
        )
        assert got.binding.connection.resource_id == OTHER_CHANNEL_ID

    async def test_empty_list(self, pc_service: ProviderConnectionService, pc_db: Any) -> None:
        assert (
            await pc_service.list(
                tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id
            )
            == []
        )

    async def test_unknown_id(self, pc_service: ProviderConnectionService, pc_db: Any) -> None:
        with pytest.raises(ConnectionNotFound):
            await pc_service.get(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=999,
            )

    async def test_a_row_whose_provider_has_no_adapter_is_listed_not_hidden(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await create_twitch(pc_service, pc_db, bot_tokens)
        from services.provider_connections.adapter import AdapterRegistry

        orphaned = ProviderConnectionService(
            pc_db.install_dal, AdapterRegistry(), PlatformKeyVault(), clock=clock
        )
        with caplog.at_level("WARNING"):
            listed = await orphaned.list(
                tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id
            )
        assert len(listed) == 1
        assert listed[0].spec is None and not listed[0].usable
        assert listed[0].public_metadata == {} and listed[0].capabilities == []
        assert "unregistered_provider" in caplog.text


class TestUpdate:
    async def test_enabled_toggle_is_per_community_and_leaves_the_link_alone(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        before = (await raw_rows(pc_db, "platform_connections"))[0]
        updated = await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=cid,
            enabled=False,
            metadata=None,
            access_token=None,
            refresh_token=None,
        )
        assert updated.binding.grant.enabled is False and not updated.usable
        after = (await raw_rows(pc_db, "platform_connections"))[0]
        assert {k: v for k, v in after.items()} == {k: v for k, v in before.items()}

    async def test_toggle_works_on_a_shared_link_without_touching_the_other_community(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid)
        await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=cid,
            enabled=False,
            metadata=None,
            access_token=None,
            refresh_token=None,
        )
        other = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.second_community_id,
            connection_id=cid,
        )
        assert other.binding.grant.enabled is True and other.usable

    async def test_retarget_to_another_channel_changes_the_resource(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        updated = await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=view.binding.connection.id,
            enabled=None,
            metadata={"channel_login": OTHER_CHANNEL_LOGIN},
            access_token=None,
            refresh_token=None,
        )
        assert updated.binding.connection.resource_id == OTHER_CHANNEL_ID
        assert updated.public_metadata["channel_login"] == OTHER_CHANNEL_LOGIN
        # The bot facts survive: only the channel moved.
        assert updated.public_metadata["bot_login"] == BOT.login

    async def test_retarget_onto_a_resource_already_connected_conflicts_and_changes_nothing(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        first = await create_twitch(pc_service, pc_db, bot_tokens)
        access, refresh = fake_twitch.mint(account=BOT)
        await create_twitch(pc_service, pc_db, (access, refresh), channel=OTHER_CHANNEL_LOGIN)
        with pytest.raises(ConnectionConflict, match="already connected"):
            await pc_service.update(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=1,
                connection_id=first.binding.connection.id,
                enabled=None,
                metadata={"channel_login": OTHER_CHANNEL_LOGIN},
                access_token=None,
                refresh_token=None,
            )
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert row["resource_id"] == CHANNEL_ID

    async def test_rotating_credentials_replaces_both_tokens_and_revalidates(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        new_access, new_refresh = fake_twitch.mint(account=BOT, expires_in=3000)
        updated = await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=view.binding.connection.id,
            enabled=None,
            metadata=None,
            access_token=new_access,
            refresh_token=new_refresh,
        )
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert open_row(row) == new_access
        assert open_row(row, "refresh_token") == new_refresh
        assert row["seal_version"] == 1
        assert updated.binding.connection.resource_id == CHANNEL_ID
        assert row["status"] == "active" and row["status_reason"] is None

    async def test_rotation_with_a_token_the_provider_rejects_changes_nothing(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        before = await raw_rows(pc_db, "platform_connections")
        with pytest.raises(CredentialsRejected):
            await pc_service.update(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=1,
                connection_id=view.binding.connection.id,
                enabled=None,
                metadata=None,
                access_token="bogus-token",
                refresh_token=None,
            )
        assert await raw_rows(pc_db, "platform_connections") == before

    async def test_a_shared_link_cannot_be_rotated_or_retargeted(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid)
        before = await raw_rows(pc_db, "platform_connections")
        new_access, _ = fake_twitch.mint(account=BOT)
        for kwargs in (
            {"access_token": new_access, "refresh_token": None, "metadata": None},
            {
                "access_token": None,
                "refresh_token": None,
                "metadata": {"channel_login": OTHER_CHANNEL_LOGIN},
            },
        ):
            with pytest.raises(ConnectionConflict, match="shared with other communities"):
                await pc_service.update(
                    tenant_id=pc_db.seed.tenant_id,
                    community_id=pc_db.seed.community_id,
                    actor_user_id=1,
                    connection_id=cid,
                    enabled=None,
                    **kwargs,
                )
        assert await raw_rows(pc_db, "platform_connections") == before

    async def test_metadata_only_update_uses_a_refreshed_token_when_the_stored_one_is_stale(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        clock: FakeClock,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        clock.advance(seconds=14000)  # stored access token is now at its expiry
        # The fake only honours un-expired tokens it minted; a refresh mints a working replacement.
        updated = await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=view.binding.connection.id,
            enabled=None,
            metadata={"channel_login": OTHER_CHANNEL_LOGIN},
            access_token=None,
            refresh_token=None,
        )
        assert fake_twitch.refresh_calls == 1
        assert updated.binding.connection.resource_id == OTHER_CHANNEL_ID

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"refresh_token": "orphan-refresh"},
        ],
        ids=["nothing-to-update", "refresh-without-access"],
    )
    async def test_empty_or_inconsistent_patch_is_refused(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        kwargs: dict[str, Any],
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        params: dict[str, Any] = {
            "enabled": None,
            "metadata": None,
            "access_token": None,
            "refresh_token": None,
        }
        params.update(kwargs)
        with pytest.raises(InvalidConnectionInput):
            await pc_service.update(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=1,
                connection_id=view.binding.connection.id,
                **params,
            )

    async def test_update_of_an_unknown_connection(
        self, pc_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        with pytest.raises(ConnectionNotFound):
            await pc_service.update(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=1,
                connection_id=404,
                enabled=True,
                metadata=None,
                access_token=None,
                refresh_token=None,
            )

    async def test_a_noop_enabled_patch_succeeds(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        again = await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=view.binding.connection.id,
            enabled=True,
            metadata=None,
            access_token=None,
            refresh_token=None,
        )
        assert again.binding.grant.enabled is True


class TestDelete:
    async def test_deleting_the_only_grant_purges_the_link_and_its_tokens(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        purged = await pc_service.delete(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            connection_id=view.binding.connection.id,
        )
        assert purged is True
        assert await raw_rows(pc_db, "platform_connections") == []
        assert await raw_rows(pc_db, "community_connection_access") == []
        with pytest.raises(ConnectionNotFound):
            await pc_service.get(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=view.binding.connection.id,
            )

    async def test_deleting_one_of_two_grants_keeps_the_link_for_the_other_community(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid)
        assert (
            await pc_service.delete(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=cid,
            )
            is False
        )
        remaining = await raw_rows(pc_db, "platform_connections")
        assert len(remaining) == 1
        assert open_row(remaining[0]) == bot_tokens[0]
        survivor = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.second_community_id,
            connection_id=cid,
        )
        assert survivor.binding.shared_with == 0 and survivor.usable

    async def test_deleting_a_missing_connection(
        self, pc_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        with pytest.raises(ConnectionNotFound):
            await pc_service.delete(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=1,
            )

    async def test_recreating_after_delete_works(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        await pc_service.delete(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            connection_id=view.binding.connection.id,
        )
        access, refresh = fake_twitch.mint(account=BOT)
        again = await create_twitch(pc_service, pc_db, (access, refresh))
        assert again.binding.connection.resource_id == CHANNEL_ID
