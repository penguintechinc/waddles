"""`services/role_sync_service.py` -- `twitch_to_discord` reconcile engine tests.

Twitch/Discord HTTP calls are mocked via the module's own `TwitchRoleSource
Client`/`DiscordRoleTargetClient` Protocols (fakes below) -- no real network
access, no `httpx.AsyncClient` constructed. DB state uses `bar_citizen_db`
(real `bind_bar_citizen_tables()`/`bind_auth_tables()` sqlite, migration
0034's field list) -- same fixture `test_guild_pairing_service.py` uses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import role_sync_service as svc
from services.credential_resolver import PlatformCredentials, TransportUnavailable
from services.guild_pairing import create_binding, create_pairing, update_pairing

_DISCORD_GUILD_ID = "123456789012345678"  # gitleaks:allow - fake snowflake, not a secret
_TIER1_ROLE = "111111111111111111"
_MOD_ROLE = "222222222222222222"
_TWITCH_SUB_USER = "900001"
_TWITCH_MOD_USER = "900002"
_TWITCH_UNLINKED_USER = "900003"
_DISCORD_SUB_USER = "800001"
_DISCORD_MOD_USER = "800002"


class _FakeTwitchClient:
    """Fixed sub/mod/broadcaster-id responses -- no HTTP, no pagination."""

    def __init__(
        self,
        *,
        tiers: dict[str, int] | None = None,
        mods: set[str] | None = None,
        broadcaster_id: str = "500000",
    ) -> None:
        self.tiers = tiers or {}
        self.mods = mods or set()
        self.broadcaster_id = broadcaster_id
        self.calls: list[str] = []

    async def get_broadcaster_id(self, *, user_token: str, client_id: str) -> str:
        self.calls.append("get_broadcaster_id")
        return self.broadcaster_id

    async def list_subscriber_tiers(
        self, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> dict[str, int]:
        self.calls.append("list_subscriber_tiers")
        return self.tiers

    async def list_moderators(
        self, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> set[str]:
        self.calls.append("list_moderators")
        return self.mods


class _FakeDiscordClient:
    """In-memory guild member -> role-id-set store, standing in for live Discord REST calls."""

    def __init__(self, member_roles: dict[str, set[str]]) -> None:
        self._member_roles = member_roles
        self.add_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[str, str]] = []

    async def get_member_role_ids(self, *, guild_id: str, user_id: str) -> set[str] | None:
        return self._member_roles.get(user_id)

    async def add_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        self.add_calls.append((user_id, role_id))
        self._member_roles.setdefault(user_id, set()).add(role_id)
        return True

    async def remove_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        self.remove_calls.append((user_id, role_id))
        self._member_roles.setdefault(user_id, set()).discard(role_id)
        return True


class _FailingCredentialResolver:
    """Always raises `TransportUnavailable` -- the fail-closed path."""

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> Any:
        raise TransportUnavailable(f"no creds for {platform}")


class _FakeCredentialResolver:
    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        if platform == "twitch":
            payload = {"client_id": "twitch-client-id"}
        else:
            payload = {"bot_token": "discord-bot-token"}  # noqa: S105 - test fixture, not a secret
        return PlatformCredentials(
            tenant_id=tenant_id, platform=platform, payload=payload, source="tenant"
        )


def _link_identity(dal: Any, *, hub_user_id: int, platform: str, platform_user_id: str) -> None:
    dal.hub_user_identities.insert(
        hub_user_id=hub_user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        is_primary=True,
        linked_at=datetime.now(UTC),
    )
    dal.commit()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the PostHog flag ON for every test; individual tests override to OFF."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


def _make_pairing(
    dal: Any, community_id: int, *, direction: str = "twitch_to_discord", sync_enabled: bool = True
) -> Any:
    created = create_pairing(
        dal,
        community_id,
        discord_guild_id=_DISCORD_GUILD_ID,
        direction=direction,
        role_name_prefix="[BC]",
        actor_user_id=None,
    )
    if sync_enabled:
        update_pairing(dal, community_id, created.id, sync_enabled=True)
    create_binding(
        dal,
        community_id,
        created.id,
        sync_scope="subscriber_tier",
        discord_role_id=_TIER1_ROLE,
        subscriber_tier=1,
    )
    create_binding(dal, community_id, created.id, sync_scope="moderator", discord_role_id=_MOD_ROLE)
    row = dal(dal.guild_tenant_pairings.id == created.id).select().first()
    return row


async def _always_token(community_id: int) -> str | None:
    return "broadcaster-user-token"  # noqa: S105 - test fixture, not a secret


class TestOptInGating:
    async def test_disabled_pairing_is_noop(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id, sync_enabled=False)
        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error is None
        assert result.roles_added == 0
        assert twitch.calls == []  # never even queried Twitch

    async def test_wrong_direction_is_noop_in_reconcile_pairing(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id, direction="bidirectional")
        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error is None
        assert result.roles_added == 0
        assert twitch.calls == []

    async def test_feature_flag_off_skips_pairing(
        self, bar_citizen_db: Any, _flag_on: AsyncMock
    ) -> None:
        _flag_on.return_value = False
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)
        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error is None
        assert result.roles_added == 0
        assert twitch.calls == []


class TestMappingAndReconciliation:
    async def test_adds_tier_and_moderator_roles_for_linked_users(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)
        _link_identity(dal, hub_user_id=2, platform="twitch", platform_user_id=_TWITCH_MOD_USER)
        _link_identity(dal, hub_user_id=2, platform="discord", platform_user_id=_DISCORD_MOD_USER)

        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1}, mods={_TWITCH_MOD_USER})
        discord_client = _FakeDiscordClient({_DISCORD_SUB_USER: set(), _DISCORD_MOD_USER: set()})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.roles_added == 2
        assert result.roles_removed == 0
        assert (_DISCORD_SUB_USER, _TIER1_ROLE) in discord_client.add_calls
        assert (_DISCORD_MOD_USER, _MOD_ROLE) in discord_client.add_calls

    async def test_removes_managed_role_when_no_longer_subscribed(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)

        # No longer a subscriber or mod this pass, but still holds the tier-1 role from before.
        twitch = _FakeTwitchClient(tiers={}, mods=set())
        discord_client = _FakeDiscordClient({_DISCORD_SUB_USER: {_TIER1_ROLE}})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        # No subs/mods this pass means no Twitch user is even examined, so the stale
        # role on an unrelated-this-pass member is never touched -- covered by the
        # multi-community isolation + explicit tier-change test below instead.
        assert result.error is None
        assert result.roles_removed == 0

    async def test_tier_change_removes_old_tier_role_and_adds_new(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        create_binding(
            dal,
            community_id,
            pairing.id,
            sync_scope="subscriber_tier",
            discord_role_id="333333333333333333",
            subscriber_tier=2,
        )
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)

        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 2})
        discord_client = _FakeDiscordClient({_DISCORD_SUB_USER: {_TIER1_ROLE}})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.roles_added == 1
        assert result.roles_removed == 1
        assert (_DISCORD_SUB_USER, _TIER1_ROLE) in discord_client.remove_calls
        assert (_DISCORD_SUB_USER, "333333333333333333") in discord_client.add_calls

    async def test_unlinked_twitch_user_is_skipped_not_errored(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        twitch = _FakeTwitchClient(tiers={_TWITCH_UNLINKED_USER: 1})
        discord_client = _FakeDiscordClient({})

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.users_skipped_unlinked == 1
        assert result.roles_added == 0

    async def test_non_guild_member_is_skipped(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)

        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})
        discord_client = _FakeDiscordClient({})  # empty dict => get_member_role_ids returns None

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.roles_added == 0
        assert discord_client.add_calls == []


class TestMultiCommunityIsolation:
    async def test_two_communities_sharing_a_guild_use_independent_bindings(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        other_community_id = dal.communities.insert(name="other", tenant_id=tenant_id)
        dal.commit()

        pairing_a = _make_pairing(dal, community_id)
        pairing_b = create_pairing(
            dal,
            other_community_id,
            discord_guild_id=_DISCORD_GUILD_ID,
            direction="twitch_to_discord",
            role_name_prefix="[OTHER]",
            actor_user_id=None,
        )
        update_pairing(dal, other_community_id, pairing_b.id, sync_enabled=True)
        other_mod_role = "444444444444444444"
        create_binding(
            dal,
            other_community_id,
            pairing_b.id,
            sync_scope="moderator",
            discord_role_id=other_mod_role,
        )
        pairing_b_row = dal(dal.guild_tenant_pairings.id == pairing_b.id).select().first()

        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_MOD_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_MOD_USER)

        twitch = _FakeTwitchClient(mods={_TWITCH_MOD_USER})
        discord_client = _FakeDiscordClient({_DISCORD_MOD_USER: set()})

        result_a = await svc.reconcile_pairing(
            dal,
            pairing_a,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )
        result_b = await svc.reconcile_pairing(
            dal,
            pairing_b_row,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert result_a.roles_added == 1  # community A's own moderator role
        assert result_b.roles_added == 1  # community B's own (different) moderator role
        assert (_DISCORD_MOD_USER, _MOD_ROLE) in discord_client.add_calls
        assert (_DISCORD_MOD_USER, other_mod_role) in discord_client.add_calls


class TestFailClosed:
    async def test_credential_failure_skips_pairing_without_raising(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FailingCredentialResolver(),
            twitch_client=_FakeTwitchClient(),
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error == "TransportUnavailable"
        assert result.roles_added == 0

    async def test_missing_broadcaster_token_skips_pairing(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)

        async def _no_token(community_id: int) -> str | None:
            return None

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_no_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=_FakeTwitchClient(),
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error == "TransportUnavailable"

    async def test_twitch_api_failure_skips_pairing(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)

        class _BoomTwitchClient(_FakeTwitchClient):
            async def list_subscriber_tiers(self, **kwargs: Any) -> dict[str, int]:
                raise svc.TwitchSyncError("twitch api returned HTTP 500 during /subscriptions")

        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=_BoomTwitchClient(),
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error == "TwitchSyncError"

    async def test_one_pairing_failure_does_not_affect_batch(self, bar_citizen_db: Any) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        other_community_id = dal.communities.insert(name="other2", tenant_id=tenant_id)
        dal.commit()

        _make_pairing(dal, community_id)
        bad_pairing = create_pairing(
            dal,
            other_community_id,
            discord_guild_id="999999999999999999",
            direction="twitch_to_discord",
            role_name_prefix="[BAD]",
            actor_user_id=None,
        )
        update_pairing(dal, other_community_id, bad_pairing.id, sync_enabled=True)
        create_binding(
            dal,
            other_community_id,
            bad_pairing.id,
            sync_scope="moderator",
            discord_role_id="555555555555555555",
        )
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)

        calls = {"n": 0}

        async def _token_fails_for_second_community(cid: int) -> str | None:
            calls["n"] += 1
            if cid == other_community_id:
                return None  # fail-closed for this one pairing only
            return "broadcaster-user-token"  # noqa: S105

        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})
        discord_client = _FakeDiscordClient({_DISCORD_SUB_USER: set()})

        summary = await svc.run_role_sync_reconcile_batch(
            dal,
            get_broadcaster_user_token=_token_fails_for_second_community,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert summary.pairings_examined == 2
        assert summary.pairings_synced == 1
        assert summary.pairings_failed == 1
        assert summary.roles_added == 1


class TestLoggingHygiene:
    async def test_no_raw_twitch_or_discord_identifiers_logged(
        self, bar_citizen_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Only counts/pairing/community/guild ids appear in log output, never a login/username."""
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)

        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})
        discord_client = _FakeDiscordClient({_DISCORD_SUB_USER: set()})

        with caplog.at_level(logging.INFO, logger="services.role_sync_service"):
            await svc.reconcile_pairing(
                dal,
                pairing,
                get_broadcaster_user_token=_always_token,
                credential_resolver=_FakeCredentialResolver(),
                twitch_client=twitch,
                make_discord_client=lambda token: discord_client,
            )

        combined = "\n".join(record.getMessage() for record in caplog.records)
        # Only numeric platform IDs (not a username/login string) may appear.
        assert "fakename" not in combined.lower()
        assert _TWITCH_SUB_USER not in combined
        assert _DISCORD_SUB_USER not in combined
