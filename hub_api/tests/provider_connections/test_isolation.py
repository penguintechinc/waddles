"""Tenant and community isolation: a connection is reachable only through its own grant."""

from __future__ import annotations

from typing import Any

import pytest

from services.provider_connections import store
from services.provider_connections.errors import ConnectionNotFound
from services.provider_connections.service import ProviderConnectionService
from tests.provider_connections.conftest import OTHER_CHANNEL_LOGIN
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import create_twitch, grant_to, raw_rows


@pytest.fixture
async def acme_connection(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
) -> Any:
    return await create_twitch(pc_service, pc_db, bot_tokens)


async def _snapshot(db: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return await raw_rows(db, "platform_connections"), await raw_rows(
        db, "community_connection_access"
    )


class TestCrossTenant:
    async def test_other_tenant_sees_nothing_even_with_its_own_valid_community(
        self, pc_service: ProviderConnectionService, pc_db: Any, acme_connection: Any
    ) -> None:
        assert (
            await pc_service.list(
                tenant_id=pc_db.seed.other_tenant_id, community_id=pc_db.seed.other_community_id
            )
            == []
        )

    @pytest.mark.parametrize("operation", ["get", "delete", "update"])
    async def test_other_tenant_cannot_address_a_connection_by_id(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        acme_connection: Any,
        operation: str,
    ) -> None:
        before = await _snapshot(pc_db)
        cid = acme_connection.binding.connection.id
        scope = {
            "tenant_id": pc_db.seed.other_tenant_id,
            "community_id": pc_db.seed.other_community_id,
        }
        with pytest.raises(ConnectionNotFound):
            if operation == "get":
                await pc_service.get(connection_id=cid, **scope)
            elif operation == "delete":
                await pc_service.delete(connection_id=cid, **scope)
            else:
                await pc_service.update(
                    connection_id=cid,
                    actor_user_id=1,
                    enabled=False,
                    metadata=None,
                    access_token=None,
                    refresh_token=None,
                    **scope,
                )
        assert await _snapshot(pc_db) == before

    @pytest.mark.parametrize("operation", ["get", "delete"])
    async def test_a_foreign_community_id_cannot_be_paired_with_my_tenant(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        acme_connection: Any,
        operation: str,
    ) -> None:
        """My tenant + the victim's community id is the classic IDOR shape; both halves must match."""
        before = await _snapshot(pc_db)
        cid = acme_connection.binding.connection.id
        with pytest.raises(ConnectionNotFound):
            if operation == "get":
                await pc_service.get(
                    tenant_id=pc_db.seed.other_tenant_id,
                    community_id=pc_db.seed.community_id,
                    connection_id=cid,
                )
            else:
                await pc_service.delete(
                    tenant_id=pc_db.seed.other_tenant_id,
                    community_id=pc_db.seed.community_id,
                    connection_id=cid,
                )
        assert await _snapshot(pc_db) == before

    async def test_listing_with_my_tenant_and_the_victims_community_returns_nothing(
        self, pc_service: ProviderConnectionService, pc_db: Any, acme_connection: Any
    ) -> None:
        """Both halves of the scope must match: a foreign tenant id never unlocks a community's list."""
        assert (
            await pc_service.list(
                tenant_id=pc_db.seed.other_tenant_id, community_id=pc_db.seed.community_id
            )
            == []
        )

    async def test_creating_for_a_community_of_another_tenant_is_refused_before_any_provider_call(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        with pytest.raises(ConnectionNotFound):
            await create_twitch(
                pc_service,
                pc_db,
                bot_tokens,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.other_community_id,
            )
        assert fake_twitch.calls == []  # no provider quota or credential exposure spent
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_the_store_rechecks_ownership_even_if_the_service_check_is_bypassed(
        self, pc_db: Any
    ) -> None:
        """Defence in depth: ``insert_connection`` re-verifies community->tenant in its transaction."""
        from datetime import UTC, datetime
        from uuid import uuid4

        with pytest.raises(ConnectionNotFound):
            await store.insert_connection(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.other_community_id,
                platform="twitch",
                resource_type="twitch_channel",
                resource_id="1",
                public_id=uuid4(),
                secrets=store.SealedSecrets("a", None),
                expires_at=None,
                resource_metadata={},
                installed_by_user_id=None,
                enabled=True,
                now=datetime.now(UTC),
            )
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_community_tenant_lookup(self, pc_db: Any) -> None:
        assert await store.community_tenant_id(pc_db.install_dal, pc_db.seed.community_id) == (
            pc_db.seed.tenant_id
        )
        assert await store.community_tenant_id(pc_db.install_dal, 99999) is None


class TestCrossCommunity:
    async def test_a_sibling_community_in_the_same_tenant_cannot_see_or_touch_it(
        self, pc_service: ProviderConnectionService, pc_db: Any, acme_connection: Any
    ) -> None:
        before = await _snapshot(pc_db)
        sibling = {
            "tenant_id": pc_db.seed.tenant_id,
            "community_id": pc_db.seed.second_community_id,
        }
        cid = acme_connection.binding.connection.id
        assert await pc_service.list(**sibling) == []
        with pytest.raises(ConnectionNotFound):
            await pc_service.get(connection_id=cid, **sibling)
        with pytest.raises(ConnectionNotFound):
            await pc_service.delete(connection_id=cid, **sibling)
        with pytest.raises(ConnectionNotFound):
            await pc_service.update(
                connection_id=cid,
                actor_user_id=1,
                enabled=False,
                metadata=None,
                access_token=None,
                refresh_token=None,
                **sibling,
            )
        assert await _snapshot(pc_db) == before

    async def test_a_revoked_grant_hides_the_connection_from_that_community(
        self, pc_service: ProviderConnectionService, pc_db: Any, acme_connection: Any
    ) -> None:
        cid = acme_connection.binding.connection.id
        await grant_to(
            pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid, status="revoked"
        )
        sibling = {
            "tenant_id": pc_db.seed.tenant_id,
            "community_id": pc_db.seed.second_community_id,
        }
        assert await pc_service.list(**sibling) == []
        with pytest.raises(ConnectionNotFound):
            await pc_service.get(connection_id=cid, **sibling)
        # ...and a revoked grant does not count as "sharing" the link.
        original = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id, connection_id=cid
        )
        assert original.binding.shared_with == 0

    async def test_a_pending_grant_is_visible_to_its_community_but_not_usable(
        self, pc_service: ProviderConnectionService, pc_db: Any, acme_connection: Any
    ) -> None:
        cid = acme_connection.binding.connection.id
        await grant_to(
            pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid, status="pending"
        )
        view = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.second_community_id,
            connection_id=cid,
        )
        assert view.binding.grant.status == "pending"
        assert not view.usable
        # Pending still counts as sharing: the owner may not rewrite a link someone else asked for.
        owner = await pc_service.get(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id, connection_id=cid
        )
        assert owner.binding.shared_with == 1

    async def test_two_communities_each_see_only_their_own_connections(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        acme_connection: Any,
    ) -> None:
        access, refresh = fake_twitch.mint(account=fake_twitch.tokens[bot_tokens[0]].account)
        second = await create_twitch(
            pc_service,
            pc_db,
            (access, refresh),
            community_id=pc_db.seed.second_community_id,
            channel=OTHER_CHANNEL_LOGIN,
        )
        mine = await pc_service.list(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.community_id
        )
        theirs = await pc_service.list(
            tenant_id=pc_db.seed.tenant_id, community_id=pc_db.seed.second_community_id
        )
        assert [v.binding.connection.id for v in mine] == [acme_connection.binding.connection.id]
        assert [v.binding.connection.id for v in theirs] == [second.binding.connection.id]
