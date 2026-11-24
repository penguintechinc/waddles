"""Proof of channel ownership at bind time (security finding: unverified channel binding).

A Twitch channel binds only when the token owner IS the broadcaster, or the installing hub user has
OAuth-linked the broadcaster's Twitch account (``hub_user_identities``). Anything else is a 403
``ownership_not_proven``. One channel may be bound by many communities across tenants (N:M) as long
as each binder proves ownership; a duplicate bind of the same channel into the same tenant is 409.
The data plane sees ``ownership_verified`` on every binding.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.provider_connections.errors import ConnectionConflict, OwnershipNotProven
from services.provider_connections.service import ProviderConnectionService
from tests.provider_connections.conftest import BOT, CHANNEL_ID, CHANNEL_LOGIN, OTHER_CHANNEL_LOGIN
from tests.provider_connections.fake_google import broadcast
from tests.provider_connections.fake_twitch import FakeAccount, FakeTwitch
from tests.provider_connections.support import create_twitch, raw_rows
from tests.provider_connections.youtube_fixtures import YT_CHAT, YT_OTHER_CHANNEL


def _unlinked_user(db: Any) -> int:
    """A hub user with NO linked platform accounts (a would-be squatter)."""
    dal = db.dal
    user_id = int(dal.hub_users.insert(username=f"squatter-{dal.hub_users.id.max() or 0}"))
    dal.commit()
    return user_id


async def test_owner_with_linked_account_binds_verified(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    view = await create_twitch(pc_service, pc_db, bot_tokens)
    assert view.binding.connection.ownership_verified is True
    rows = await raw_rows(pc_db, "platform_connections")
    assert [bool(r["ownership_verified"]) for r in rows] == [True]


async def test_non_owner_is_refused_403_and_nothing_is_stored(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    squatter = _unlinked_user(pc_db)
    with pytest.raises(OwnershipNotProven) as err:
        await create_twitch(pc_service, pc_db, bot_tokens, actor_user_id=squatter)
    assert err.value.code == "ownership_not_proven"
    assert await raw_rows(pc_db, "platform_connections") == []


async def test_linked_account_of_a_different_channel_is_not_proof(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    user = _unlinked_user(pc_db)
    pc_db.dal.hub_user_identities.insert(
        hub_user_id=user, platform="twitch", platform_user_id="424242"
    )
    pc_db.dal.commit()
    with pytest.raises(OwnershipNotProven):
        await create_twitch(pc_service, pc_db, bot_tokens, actor_user_id=user)


async def test_broadcaster_own_token_is_proof_without_a_linked_account(
    pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
) -> None:
    tokens = fake_twitch.mint(account=FakeAccount(user_id=CHANNEL_ID, login=CHANNEL_LOGIN))
    view = await create_twitch(pc_service, pc_db, tokens, actor_user_id=_unlinked_user(pc_db))
    assert view.binding.connection.ownership_verified is True


async def test_same_channel_twice_in_one_tenant_is_409(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    await create_twitch(pc_service, pc_db, bot_tokens)
    with pytest.raises(ConnectionConflict):
        await create_twitch(pc_service, pc_db, bot_tokens)


async def test_verified_owner_may_bind_the_channel_into_another_tenant_n_to_m(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    await create_twitch(pc_service, pc_db, bot_tokens)
    other = await create_twitch(
        pc_service,
        pc_db,
        bot_tokens,
        tenant_id=pc_db.seed.other_tenant_id,
        community_id=pc_db.seed.other_community_id,
    )
    assert other.binding.connection.ownership_verified is True
    rows = await raw_rows(pc_db, "platform_connections")
    assert sorted(r["tenant_id"] for r in rows) == sorted(
        [pc_db.seed.tenant_id, pc_db.seed.other_tenant_id]
    )


async def test_non_owner_cannot_retarget_an_existing_connection(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    view = await create_twitch(pc_service, pc_db, bot_tokens)
    with pytest.raises(OwnershipNotProven):
        await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=_unlinked_user(pc_db),
            connection_id=view.binding.connection.id,
            enabled=None,
            metadata={"channel_login": OTHER_CHANNEL_LOGIN},
            access_token=None,
            refresh_token=None,
        )


async def test_dataplane_binding_carries_the_verified_flag(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    await create_twitch(pc_service, pc_db, bot_tokens)
    page = await pc_service.list_for_dataplane(
        provider="twitch", capability=None, after=0, limit=10
    )
    assert [item.metadata["ownership_verified"] for item in page.items] == ["true"]


async def test_bot_identity_is_not_confused_with_the_owner(
    pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: tuple[str, str | None]
) -> None:
    """The bot token's own id (``BOT``) never satisfies ownership of someone else's channel."""
    user = _unlinked_user(pc_db)
    pc_db.dal.hub_user_identities.insert(
        hub_user_id=user, platform="twitch", platform_user_id=BOT.user_id
    )
    pc_db.dal.commit()
    with pytest.raises(OwnershipNotProven):
        await create_twitch(pc_service, pc_db, bot_tokens, actor_user_id=user)


async def test_youtube_foreign_live_chat_id_is_rejected_403(
    yt_service: ProviderConnectionService, yt_db: Any, fake_google: Any, yt_tokens: Any
) -> None:
    """Another channel's live chat (even one the fake knows) never binds to this connection."""
    fake_google.broadcasts[YT_OTHER_CHANNEL.channel_id] = [
        broadcast("ForeignChat123", channel_id=YT_OTHER_CHANNEL.channel_id)
    ]
    with pytest.raises(OwnershipNotProven) as err:
        await yt_service.create(
            tenant_id=yt_db.seed.tenant_id,
            community_id=yt_db.seed.community_id,
            actor_user_id=yt_db.seed.admin_user_id,
            provider="youtube",
            access_token=yt_tokens[0],
            refresh_token=yt_tokens[1],
            metadata={"live_chat_id": "ForeignChat123"},
            enabled=True,
        )
    assert err.value.code == "live_chat_not_owned"
    assert await raw_rows(yt_db, "platform_connections") == []


async def test_youtube_own_live_chat_id_binds_verified(
    yt_service: ProviderConnectionService, yt_db: Any, yt_tokens: Any
) -> None:
    view = await yt_service.create(
        tenant_id=yt_db.seed.tenant_id,
        community_id=yt_db.seed.community_id,
        actor_user_id=None,
        provider="youtube",
        access_token=yt_tokens[0],
        refresh_token=yt_tokens[1],
        metadata={"live_chat_id": YT_CHAT},
        enabled=True,
    )
    assert view.binding.connection.ownership_verified is True
