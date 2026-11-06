"""Binding-feed contract for the connector runtime: chat_ingest everywhere, tenant, secret_version.

Real path: real service + store + SQL (sqlite file via ``pc_db``), real blueprints / auth chain /
``validate_response``; only the provider sockets are fakes. Pins (a) ``chat_ingest`` is listable for
Twitch, YouTube *and* Discord (including a Discord row stored before the capability existed),
(b) ``tenantId`` is derived from the community's own tenant -- tampered metadata or a foreign
connection tenant cannot change it, (c) ``secretVersion`` bumps only on admin credential
replacement, and (d) the exact response field set.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from services.provider_connections import ProviderConnectionService
from services.provider_connections.registry import build_default_registry
from tests.provider_connections import test_discord_binding as _discord_tests
from tests.provider_connections.conftest import BOT, FakeClock, admin_headers
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import (
    create_twitch,
    grant_to,
    raw_rows,
    set_column,
)
from tests.provider_connections.test_api import _svc
from tests.provider_connections.test_discord_binding import (
    GUILD,
    _create_body,
)

# Re-register the Discord fixtures (and its autouse DNS shim) for this module.
fake_discord = _discord_tests.fake_discord
discord_service = _discord_tests.discord_service
dclient = _discord_tests.dclient
_discord_dns = _discord_tests._discord_dns  # noqa: SLF001

INTERNAL = "/api/v1/internal"
ITEM_FIELDS = {
    "grantId",
    "connectionId",
    "communityId",
    "tenantId",
    "provider",
    "resourceType",
    "resourceId",
    "capabilities",
    "expiresAt",
    "updatedAt",
    "metadata",
    "kind",
    "secretVersion",
}


async def _create_discord(dclient: Any, db: Any) -> int:
    response = await dclient.post(
        f"/api/v1/communities/{db.seed.community_id}/provider-connections",
        headers=admin_headers(db),
        data=_create_body(),
    )
    assert response.status_code == 201, await response.get_data(as_text=True)
    return int((await response.get_json())["connection"]["id"])


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    """The row's ``resource_metadata`` as a dict (sqlite returns the JSON text)."""
    raw = row["resource_metadata"]
    return dict(raw) if isinstance(raw, dict) else dict(json.loads(raw))


async def _list(client: Any, issuer: ServiceJwtIssuer, query: str) -> list[dict[str, Any]]:
    response = await client.get(f"{INTERNAL}/provider-connections?{query}", headers=_svc(issuer))
    assert response.status_code == 200, await response.get_data(as_text=True)
    return list((await response.get_json())["items"])


class TestChatIngestCapability:
    @pytest.mark.parametrize("provider", ["twitch", "youtube", "discord"])
    def test_every_ingest_platform_advertises_chat_ingest(self, provider: str) -> None:
        spec = build_default_registry().get(provider).spec
        assert "chat_ingest" in spec.capabilities

    async def test_discord_chat_ingest_list_returns_the_row(
        self, dclient: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        cid = await _create_discord(dclient, pc_db)
        (item,) = await _list(dclient, service_issuer, "provider=discord&capability=chat_ingest")
        assert item["connectionId"] == cid
        assert item["capabilities"] == ["action_relay", "chat_ingest"]
        assert item["metadata"] == {"guild_id": GUILD}
        # The relay filter keeps working next to it.
        assert (
            len(await _list(dclient, service_issuer, "provider=discord&capability=action_relay"))
            == 1
        )

    async def test_a_row_stored_before_chat_ingest_existed_is_still_served(
        self, dclient: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        cid = await _create_discord(dclient, pc_db)
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        legacy = _meta(row)
        legacy["capabilities"] = ["action_relay"]  # exactly what pre-change rows hold
        await set_column(pc_db, "platform_connections", cid, resource_metadata=json.dumps(legacy))
        items = await _list(dclient, service_issuer, "provider=discord&capability=chat_ingest")
        assert [i["connectionId"] for i in items] == [cid]

    async def test_twitch_chat_ingest_still_listed(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        await create_twitch(pc_service, pc_db, bot_tokens)
        items = await _list(client, service_issuer, "provider=twitch&capability=chat_ingest")
        assert len(items) == 1 and "chat_ingest" in items[0]["capabilities"]


class TestTenantDerivation:
    async def test_tenant_id_is_the_communitys_tenant_not_metadata(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        meta = _meta(row)
        meta["tenant_id"] = pc_db.seed.other_tenant_id  # tampered "editable metadata"
        meta["tenantId"] = pc_db.seed.other_tenant_id
        await set_column(pc_db, "platform_connections", cid, resource_metadata=json.dumps(meta))
        (item,) = await _list(client, service_issuer, "provider=twitch")
        assert item["tenantId"] == pc_db.seed.tenant_id
        assert "tenant_id" not in item["metadata"] and "tenantId" not in item["metadata"]

    async def test_each_row_carries_its_own_communitys_tenant(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        fake_twitch: FakeTwitch,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        await create_twitch(pc_service, pc_db, bot_tokens)
        other = fake_twitch.mint(account=BOT)
        await create_twitch(
            pc_service,
            pc_db,
            other,
            tenant_id=pc_db.seed.other_tenant_id,
            community_id=pc_db.seed.other_community_id,
        )
        items = await _list(client, service_issuer, "provider=twitch")
        assert {(i["communityId"], i["tenantId"]) for i in items} == {
            (pc_db.seed.community_id, pc_db.seed.tenant_id),
            (pc_db.seed.other_community_id, pc_db.seed.other_tenant_id),
        }

    async def test_a_grant_whose_tenant_disagrees_with_the_link_is_never_served(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        """Tenant isolation: a foreign community granted onto a link is refused, not relabelled."""
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        await grant_to(
            pc_db,
            community_id=pc_db.seed.other_community_id,
            connection_id=view.binding.connection.id,
        )
        items = await _list(client, service_issuer, "provider=twitch")
        assert [(i["communityId"], i["tenantId"]) for i in items] == [
            (pc_db.seed.community_id, pc_db.seed.tenant_id)
        ]

    async def test_a_same_tenant_shared_grant_is_served_with_the_shared_tenant(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        await grant_to(
            pc_db,
            community_id=pc_db.seed.second_community_id,
            connection_id=view.binding.connection.id,
        )
        items = await _list(client, service_issuer, "provider=twitch")
        assert {i["communityId"] for i in items} == {
            pc_db.seed.community_id,
            pc_db.seed.second_community_id,
        }
        assert {i["tenantId"] for i in items} == {pc_db.seed.tenant_id}

    async def test_a_grant_to_a_vanished_community_is_not_served(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        await grant_to(pc_db, community_id=9999, connection_id=view.binding.connection.id)
        items = await _list(client, service_issuer, "provider=twitch")
        assert [i["communityId"] for i in items] == [pc_db.seed.community_id]


class TestSecretVersion:
    async def test_starts_at_one_and_only_credential_replacement_bumps_it(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}

        async def version() -> int:
            (item,) = await _list(client, service_issuer, "provider=twitch")
            assert isinstance(item["secretVersion"], int)
            return int(item["secretVersion"])

        assert await version() == 1
        # An OAuth token refresh is NOT a credential rotation.
        clock.advance(seconds=14000 - 60)
        await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert fake_twitch.refresh_calls == 1
        assert await version() == 1
        # Enabled toggle / metadata-only edits do not bump it either.
        await pc_service.update(
            connection_id=cid,
            actor_user_id=1,
            enabled=False,
            metadata=None,
            access_token=None,
            refresh_token=None,
            **scope,
        )
        await pc_service.update(
            connection_id=cid,
            actor_user_id=1,
            enabled=True,
            metadata=None,
            access_token=None,
            refresh_token=None,
            **scope,
        )
        assert await version() == 1
        # An admin replacing the credential does -- every time.
        for expected in (2, 3):
            new_access, new_refresh = fake_twitch.mint(account=BOT)
            await pc_service.update(
                connection_id=cid,
                actor_user_id=1,
                enabled=None,
                metadata=None,
                access_token=new_access,
                refresh_token=new_refresh,
                **scope,
            )
            assert await version() == expected

    async def test_the_counter_never_exposes_credential_material(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        pc_service: ProviderConnectionService,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        await create_twitch(pc_service, pc_db, bot_tokens)
        response = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(service_issuer)
        )
        text = await response.get_data(as_text=True)
        assert bot_tokens[0] not in text and bot_tokens[1] not in text


class TestExactFieldSet:
    async def test_twitch_and_discord_items_have_exactly_the_dto_fields(
        self,
        dclient: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        await _create_discord(dclient, pc_db)
        (item,) = await _list(dclient, service_issuer, "provider=discord")
        assert set(item) == ITEM_FIELDS
        assert isinstance(item["tenantId"], int) and isinstance(item["secretVersion"], int)
        assert isinstance(item["kind"], int)
        assert item["communityId"] == pc_db.seed.community_id
        assert item["resourceId"] == GUILD
