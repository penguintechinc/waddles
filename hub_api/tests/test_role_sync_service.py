"""`services/role_sync_service.py` -- bidirectional Twitch<->Discord reconcile engine tests.

Twitch/Discord HTTP calls are mocked via the module's own `TwitchRoleSource
Client`/`DiscordRoleTargetClient` Protocols (fakes below) -- no real network
access, no `httpx.AsyncClient` constructed. DB state uses `bar_citizen_db`
(real `bind_bar_citizen_tables()`/`bind_auth_tables()` sqlite, migration
0034/0036's field list) -- same fixture `test_guild_pairing_service.py` uses.

`TestOptInGating`/`TestMappingAndReconciliation`/`TestMultiCommunityIsolation`/
`TestFailClosed`/`TestLoggingHygiene` cover `reconcile_pairing()` (Twitch ->
Discord). `TestDiscordToPlatformDirection` covers `reconcile_pairing_discord_
to_platform()` (Discord -> this platform's community role, migration 0036).
`TestBidirectionalBatch` proves both directions run together for one
`bidirectional` pairing in a single `run_role_sync_reconcile_batch()` pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
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

    def __init__(
        self,
        member_roles: dict[str, set[str]],
        *,
        guild_members: list[svc.DiscordGuildMember] | None = None,
    ) -> None:
        self._member_roles = member_roles
        # Separate from `_member_roles` (the twitch_to_discord direction's per-user
        # current-role lookup) -- `list_guild_members` is the discord_to_platform
        # direction's own read, defaults to one `DiscordGuildMember` per
        # `member_roles` entry when not given explicitly.
        self._guild_members = guild_members or [
            svc.DiscordGuildMember(user_id=uid, role_ids=frozenset(roles))
            for uid, roles in member_roles.items()
        ]
        self.add_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.list_guild_members_calls = 0

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

    async def list_guild_members(self, *, guild_id: str) -> list[svc.DiscordGuildMember]:
        self.list_guild_members_calls += 1
        return self._guild_members


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

    async def test_bidirectional_runs_twitch_to_discord_half_in_reconcile_pairing(
        self, bar_citizen_db: Any
    ) -> None:
        """`bidirectional` runs BOTH halves -- this is the twitch_to_discord one."""
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
        assert twitch.calls != []  # bidirectional DOES run the twitch_to_discord half

    async def test_discord_to_twitch_direction_is_noop_in_reconcile_pairing(
        self, bar_citizen_db: Any
    ) -> None:
        """A pure `discord_to_twitch` pairing never runs `reconcile_pairing()` at all."""
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id, direction="discord_to_twitch")
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


# ---------------------------------------------------------------------------
# Discord -> platform direction (`reconcile_pairing_discord_to_platform`)
# ---------------------------------------------------------------------------

_COMMUNITY_ADMIN_ROLE = "333333333333333399"  # Discord role mapped -> "community-admin"
_MEMBER_ROLE_DISCORD = "444444444444444499"  # Discord role mapped -> "member"
_DISCORD_PLATFORM_USER = "800010"
_DISCORD_UNLINKED_USER = "800099"


def _add_community_member(dal: Any, *, community_id: int, hub_user_id: int, role: str) -> None:
    dal.community_members.insert(
        community_id=community_id,
        user_id=str(hub_user_id),
        role=role,
        is_active=True,
        joined_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    dal.commit()


def _get_member_role(dal: Any, *, community_id: int, hub_user_id: int) -> str:
    row = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
        )
        .select()
        .first()
    )
    assert row is not None
    return str(row.role)


def _add_community_role(dal: Any, *, community_id: int, name: str, priority: int = 0) -> None:
    dal.community_roles.insert(community_id=community_id, name=name, priority=priority)
    dal.commit()


def _make_platform_pairing(
    dal: Any,
    community_id: int,
    *,
    direction: str = "discord_to_twitch",
    guild_id: str = _DISCORD_GUILD_ID,
) -> Any:
    created = create_pairing(
        dal,
        community_id,
        discord_guild_id=guild_id,
        direction=direction,
        role_name_prefix="[BC]",
        actor_user_id=None,
    )
    update_pairing(dal, community_id, created.id, sync_enabled=True)
    create_binding(
        dal,
        community_id,
        created.id,
        sync_scope="community_role",
        discord_role_id=_MEMBER_ROLE_DISCORD,
        community_role="member",
    )
    return dal(dal.guild_tenant_pairings.id == created.id).select().first()


class TestDiscordToPlatformDirection:
    async def test_grants_mapped_community_role_to_linked_active_member(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="vip")
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.community_roles_applied == 1
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_idempotent_second_pass_applies_nothing(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="member")
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        first = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )
        second = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert first.community_roles_applied == 0  # already "member" -- unchanged, not applied
        assert second.community_roles_applied == 0
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_conflict_precedence_highest_priority_role_wins(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        create_binding(
            dal,
            community_id,
            pairing.id,
            sync_scope="community_role",
            discord_role_id=_COMMUNITY_ADMIN_ROLE,
            community_role="community-admin",
        )
        _add_community_role(dal, community_id=community_id, name="member", priority=0)
        _add_community_role(dal, community_id=community_id, name="community-admin", priority=10)
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="vip")
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER,
                    role_ids=frozenset({_MEMBER_ROLE_DISCORD, _COMMUNITY_ADMIN_ROLE}),
                )
            ],
        )

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.community_roles_applied == 1
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "community-admin"

    async def test_losing_mapped_role_never_auto_demotes(self, bar_citizen_db: Any) -> None:
        """Grant-only: a member who no longer holds any mapped Discord role keeps their role."""
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="moderator")
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset())
            ],
        )

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.community_roles_applied == 0
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "moderator"

    async def test_community_owner_is_never_touched(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="community-owner")
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.community_roles_applied == 0
        assert result.users_skipped_owner_protected == 1
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "community-owner"

    async def test_unlinked_discord_member_is_skipped_not_errored(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_UNLINKED_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.users_skipped_unlinked == 1
        assert result.community_roles_applied == 0

    async def test_linked_user_without_membership_is_skipped_not_created(
        self, bar_citizen_db: Any
    ) -> None:
        """Role-sync never creates `community_members` rows -- only adjusts an existing one."""
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.users_skipped_not_member == 1
        assert result.community_roles_applied == 0
        assert dal(dal.community_members.community_id == community_id).count() == 0

    async def test_twitch_to_discord_only_pairing_never_reads_guild_members(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id, direction="twitch_to_discord")
        discord_client = _FakeDiscordClient({})

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result.error is None
        assert result.community_roles_applied == 0
        assert discord_client.list_guild_members_calls == 0

    async def test_missing_discord_credentials_fails_closed(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)

        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FailingCredentialResolver(),
            make_discord_client=lambda token: _FakeDiscordClient({}),
        )

        assert result.error == "TransportUnavailable"
        assert result.community_roles_applied == 0

    async def test_tenant_isolation_two_communities_sharing_guild(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        other_community_id = dal.communities.insert(name="other-platform", tenant_id=tenant_id)
        dal.commit()

        pairing_a = _make_platform_pairing(dal, community_id)
        pairing_b = _make_platform_pairing(dal, other_community_id)

        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="vip")
        _add_community_member(dal, community_id=other_community_id, hub_user_id=1, role="vip")
        discord_client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        result_a = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing_a,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )
        result_b = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing_b,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: discord_client,
        )

        assert result_a.community_roles_applied == 1
        assert result_b.community_roles_applied == 1
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"
        assert _get_member_role(dal, community_id=other_community_id, hub_user_id=1) == "member"


class TestBidirectionalBatch:
    async def test_bidirectional_pairing_runs_both_directions_in_one_batch(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = _make_pairing(dal, community_id, direction="bidirectional")
        create_binding(
            dal,
            community_id,
            pairing.id,
            sync_scope="community_role",
            discord_role_id=_MEMBER_ROLE_DISCORD,
            community_role="member",
        )

        # twitch_to_discord side
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)
        # discord_to_twitch (platform) side -- a different linked hub_user
        _link_identity(
            dal, hub_user_id=2, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=2, role="vip")

        twitch = _FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1})
        discord_client = _FakeDiscordClient(
            {_DISCORD_SUB_USER: set()},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )

        summary = await svc.run_role_sync_reconcile_batch(
            dal,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=twitch,
            make_discord_client=lambda token: discord_client,
        )

        assert summary.pairings_examined == 1
        assert summary.pairings_synced == 1
        assert summary.pairings_failed == 0
        assert summary.roles_added == 1  # twitch_to_discord half
        assert summary.community_roles_applied == 1  # discord_to_twitch (platform) half
        assert _get_member_role(dal, community_id=community_id, hub_user_id=2) == "member"


class TestHttpDiscordRoleTargetClientListGuildMembers:
    """`list_guild_members()` against real `httpx` request/response objects.

    Via `httpx.MockTransport` (no real socket) -- mirrors `tests/test_calls_
    proxy.py`'s own pattern. The discord_to_platform direction's one new
    real-transport method this PR adds.
    """

    def _client(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> svc.HttpDiscordRoleTargetClient:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return svc.HttpDiscordRoleTargetClient(http_client, bot_token="test-bot-token")  # noqa: S106

    async def test_single_page_returns_members_with_role_sets(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["limit"] == str(svc._DISCORD_MEMBER_PAGE_SIZE)
            return httpx.Response(
                200,
                json=[
                    {"user": {"id": "1"}, "roles": ["10", "20"]},
                    {"user": {"id": "2"}, "roles": []},
                ],
            )

        client = self._client(handler)
        members = await client.list_guild_members(guild_id=_DISCORD_GUILD_ID)

        assert len(members) == 2
        assert svc.DiscordGuildMember(user_id="1", role_ids=frozenset({"10", "20"})) in members
        assert svc.DiscordGuildMember(user_id="2", role_ids=frozenset()) in members

    async def test_paginates_full_pages_by_last_seen_user_id(self) -> None:
        calls: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            after = request.url.params.get("after")
            calls.append(after)
            if after is None:
                page = [
                    {"user": {"id": str(i)}, "roles": []}
                    for i in range(svc._DISCORD_MEMBER_PAGE_SIZE)
                ]
                return httpx.Response(200, json=page)
            return httpx.Response(200, json=[{"user": {"id": "last"}, "roles": ["99"]}])

        client = self._client(handler)
        members = await client.list_guild_members(guild_id=_DISCORD_GUILD_ID)

        assert calls == [None, str(svc._DISCORD_MEMBER_PAGE_SIZE - 1)]
        assert len(members) == svc._DISCORD_MEMBER_PAGE_SIZE + 1
        assert svc.DiscordGuildMember(user_id="last", role_ids=frozenset({"99"})) in members

    async def test_empty_page_stops_pagination(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client = self._client(handler)
        members = await client.list_guild_members(guild_id=_DISCORD_GUILD_ID)

        assert members == []

    async def test_forbidden_response_raises_discord_sync_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Missing Access"})

        client = self._client(handler)
        with pytest.raises(svc.DiscordSyncError):
            await client.list_guild_members(guild_id=_DISCORD_GUILD_ID)

    async def test_network_error_raises_discord_sync_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom", request=request)

        client = self._client(handler)
        with pytest.raises(svc.DiscordSyncError):
            await client.list_guild_members(guild_id=_DISCORD_GUILD_ID)
