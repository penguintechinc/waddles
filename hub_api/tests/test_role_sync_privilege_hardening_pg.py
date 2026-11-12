"""Role-sync privilege hardening (S1/S2/S3 + Twitch-mod opt-in) on REAL Postgres.

Every DB read/write runs against a migrated Postgres container (head includes
0104_role_sync_priv_harden), so the CHECK constraint, the grant ledgers and the
`propagate_moderators` column are the production DDL, not a sqlite approximation.
Discord/Twitch are in-memory fakes (no network).

Skipped (never failed) where the `docker` CLI is unavailable -- treat a SKIP as a FAILURE.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import psycopg2.errors
import pytest
from flask_core.database import AsyncDAL

from services import guild_pairing as pairing_svc
from services import role_sync_inbound_service as inbound
from services import role_sync_service as svc
from services.errors import ApiError
from services.role_sync_remediation import STATUS_DEMOTED, remediate_purged_bindings
from tests import demo_pg_support as pg
from tests.test_role_sync_service import (
    _FakeCredentialResolver,
    _FakeDiscordClient,
    _FakeTwitchClient,
)

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

GUILD_ID = "123456789012345678"  # gitleaks:allow - fake snowflake
VIP_DISCORD_ROLE = "444444444444444401"
TIER1_DISCORD_ROLE = "444444444444444402"
MOD_DISCORD_ROLE = "444444444444444403"
ATTACKER = "800001"
TWITCH_MOD = "700001"
DISCORD_OF_TWITCH_MOD = "800002"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres container for the module."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-role-sync-harden") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def dal(pg_db: pg.PgTestDatabase) -> Iterator[Any]:
    """Per-test truncated, bound pydal DAL on the real container."""
    for adal in pg.fresh_dal(pg_db):
        assert isinstance(adal, AsyncDAL)
        yield adal.dal


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Role-sync kill switch ON (tests that need it OFF override)."""
    monkeypatch.delenv(svc.DEMOTE_PAIRING_IDS_ENV, raising=False)
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


class _World:
    """Tenant + community + roles + a pairing, with helpers for linked members."""

    def __init__(self, dal: Any) -> None:
        self.dal = dal
        self.tenant_id = int(
            pg.scalar(
                dal,
                "INSERT INTO tenants (slug, display_name, is_global, is_active) "
                "VALUES ('acme', 'Acme', false, true) RETURNING id",
            )
        )
        self.cid = int(
            pg.scalar(
                dal,
                "INSERT INTO communities (name, tenant_id) VALUES ('c1', %s) RETURNING id",
                [self.tenant_id],
            )
        )
        for name, prio in (("member", 0), ("vip", 5), ("moderator", 8), ("community-admin", 10)):
            pg.q(
                dal,
                "INSERT INTO community_roles (community_id, name, priority) VALUES (%s, %s, %s)",
                [self.cid, name, prio],
            )

    def pairing(
        self,
        direction: str = "discord_to_twitch",
        *,
        verified: bool = True,
        grandfathered: bool = False,
    ) -> Any:
        created = pairing_svc.create_pairing(
            self.dal,
            self.cid,
            discord_guild_id=GUILD_ID,
            direction=direction,
            role_name_prefix="[AC]",
            actor_user_id=None,
            ownership_verified=verified,
        )
        pairing_svc.update_pairing(self.dal, self.cid, created.id, sync_enabled=True)
        if grandfathered:  # what migration 0104 does to rows existing at upgrade time
            pg.q(
                self.dal,
                "UPDATE guild_tenant_pairings SET grandfathered = TRUE WHERE id = %s",
                [created.id],
            )
        return self.row(created.id)

    def row(self, pairing_id: int) -> Any:
        return self.dal(self.dal.guild_tenant_pairings.id == pairing_id).select().first()

    def member(self, discord_id: str, role: str, *, platform: str = "discord") -> int:
        hub_id = int(
            pg.scalar(
                self.dal,
                "INSERT INTO hub_users (username) VALUES (%s) RETURNING id",
                [f"u-{discord_id}"],
            )
        )
        pg.q(
            self.dal,
            "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
            "VALUES (%s, %s, %s)",
            [hub_id, platform, discord_id],
        )
        pg.q(
            self.dal,
            "INSERT INTO community_members (community_id, user_id, role, is_active) "
            "VALUES (%s, %s, %s, true)",
            [self.cid, str(hub_id), role],
        )
        return hub_id

    def role_of(self, hub_id: int) -> str:
        return str(
            pg.scalar(
                self.dal,
                "SELECT role FROM community_members WHERE community_id = %s AND user_id = %s",
                [self.cid, str(hub_id)],
            )
        )

    def grants(self) -> list[tuple[int, str, str | None]]:
        return [
            (int(r[0]), str(r[1]), r[2])
            for r in pg.q(
                self.dal,
                "SELECT hub_user_id, granted_role, prior_role FROM role_sync_community_grants "
                "ORDER BY hub_user_id",
            )
        ]


@pytest.fixture
def world(dal: Any) -> _World:
    return _World(dal)


async def _platform_pass(world: _World, pairing: Any, members: dict[str, set[str]]) -> Any:
    client = _FakeDiscordClient(
        {},
        guild_members=[
            svc.DiscordGuildMember(user_id=u, role_ids=frozenset(r)) for u, r in members.items()
        ],
    )
    return await svc.reconcile_pairing_discord_to_platform(
        world.dal,
        pairing,
        credential_resolver=_FakeCredentialResolver(),  # type: ignore[arg-type]
        make_discord_client=lambda token: client,
    )


# ----------------------------------------------------------------- S1: self-promotion


@pytest.mark.parametrize("role", ["community-admin", "moderator"])
def test_privileged_inbound_binding_rejected_by_service(world: _World, role: str) -> None:
    pairing = world.pairing()
    with pytest.raises(ApiError) as excinfo:
        pairing_svc.create_binding(
            world.dal,
            world.cid,
            pairing.id,
            sync_scope="community_role",
            discord_role_id=VIP_DISCORD_ROLE,
            community_role=role,
        )
    assert excinfo.value.status_code == 422
    assert pairing_svc.list_bindings(world.dal, world.cid, pairing.id) == []


@pytest.mark.parametrize("role", ["community-admin", "moderator"])
def test_privileged_inbound_binding_rejected_by_db_check(world: _World, role: str) -> None:
    """Even a writer that bypasses the service cannot store a privileged inbound target."""
    pairing = world.pairing()
    with pytest.raises(psycopg2.errors.CheckViolation):
        pg.q(
            world.dal,
            "INSERT INTO community_role_sync_bindings "
            "(pairing_id, sync_scope, community_role, discord_role_id) "
            "VALUES (%s, 'community_role', %s, %s)",
            [pairing.id, role, VIP_DISCORD_ROLE],
        )
    world.dal.rollback()


async def test_manage_roles_holder_cannot_self_promote(world: _World) -> None:
    """Attacker grants themselves every bound Discord role: they never get past `vip`."""
    pairing = world.pairing()
    pairing_svc.create_binding(
        world.dal,
        world.cid,
        pairing.id,
        sync_scope="community_role",
        discord_role_id=VIP_DISCORD_ROLE,
        community_role="vip",
    )
    attacker = world.member(ATTACKER, "member")
    result = await _platform_pass(world, pairing, {ATTACKER: {VIP_DISCORD_ROLE, "999"}})
    assert result.error is None
    assert world.role_of(attacker) == "vip"
    assert world.role_of(attacker) not in pairing_svc.PRIVILEGED_COMMUNITY_ROLES
    # Defense in depth: the applier itself refuses a privileged target.
    assert (
        svc._apply_community_role(
            world.dal,
            community_id=world.cid,
            hub_user_id=attacker,
            desired_role="community-admin",
            pairing_id=pairing.id,
        )
        == "privileged_refused"
    )
    assert world.role_of(attacker) == "vip"


# ----------------------------------------------------------------- S2: grant-only + own grants


async def _vip_pairing(world: _World) -> Any:
    pairing = world.pairing()
    pairing_svc.create_binding(
        world.dal,
        world.cid,
        pairing.id,
        sync_scope="community_role",
        discord_role_id=VIP_DISCORD_ROLE,
        community_role="vip",
    )
    return pairing


@pytest.mark.parametrize("admin_set", ["community-admin", "moderator"])
async def test_periodic_sync_never_demotes_admin_set_role(world: _World, admin_set: str) -> None:
    pairing = await _vip_pairing(world)
    uid = world.member(ATTACKER, admin_set)
    result = await _platform_pass(world, pairing, {ATTACKER: {VIP_DISCORD_ROLE}})
    assert result.community_roles_applied == 0
    assert world.role_of(uid) == admin_set
    assert world.grants() == []


async def test_push_path_never_demotes_admin_set_role(world: _World) -> None:
    pairing = await _vip_pairing(world)
    uid = world.member(ATTACKER, "community-admin")
    outcome = svc._apply_community_role(
        world.dal,
        community_id=world.cid,
        hub_user_id=uid,
        desired_role="vip",
        pairing_id=pairing.id,
    )
    assert outcome == "admin_set_untouched"
    assert world.role_of(uid) == "community-admin"


async def test_inbound_push_event_is_grant_only(world: _World) -> None:
    """The gateway-push applier takes the same grant-only path as the periodic pass."""
    await _vip_pairing(world)
    admin = world.member(ATTACKER, "community-admin")
    event = inbound.InboundRoleEvent(
        event_type="guild.member.update",
        tenant_id=world.tenant_id,
        community_id=world.cid,
        guild_id=GUILD_ID,
        user_id=ATTACKER,
        role_id=None,
        role_ids=frozenset({VIP_DISCORD_ROLE}),
    )

    class _Confirm:
        async def member_roles(self, dal: Any, **kw: Any) -> set[str]:
            return {VIP_DISCORD_ROLE}

    out = await inbound.apply_inbound_event(world.dal, event, _Confirm())  # type: ignore[arg-type]
    assert out.outcome == "admin_set_untouched"
    assert world.role_of(admin) == "community-admin"


async def test_grant_is_ledgered_and_only_own_grants_are_revoked(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairing = await _vip_pairing(world)
    monkeypatch.setenv(svc.DEMOTE_PAIRING_IDS_ENV, str(pairing.id))
    monkeypatch.setenv(svc.DEMOTE_BASELINE_ROLE_ENV, "member")
    granted = world.member("800010", "member")
    hand_vip = world.member("800011", "vip")  # an admin set this by hand
    keeper = world.member("800012", "member")

    first = await _platform_pass(
        world, pairing, {"800010": {VIP_DISCORD_ROLE}, "800011": set(), "800012": set()}
    )
    assert first.community_roles_applied == 1
    assert world.grants() == [(granted, "vip", "member")]

    # 800010 loses the Discord role; 800011 still has none. Only the ledgered grant is undone.
    second = await _platform_pass(
        world, pairing, {"800010": set(), "800011": set(), "800012": set()}
    )
    assert second.community_roles_demoted == 1
    assert world.role_of(granted) == "member"  # restored to its prior role
    assert world.role_of(hand_vip) == "vip"  # never sync's grant -> untouched
    assert world.role_of(keeper) == "member"
    assert world.grants() == []


async def test_revoke_skips_grant_an_admin_changed_since(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairing = await _vip_pairing(world)
    monkeypatch.setenv(svc.DEMOTE_PAIRING_IDS_ENV, str(pairing.id))
    monkeypatch.setenv(svc.DEMOTE_BASELINE_ROLE_ENV, "member")
    uid = world.member("800010", "member")
    await _platform_pass(world, pairing, {"800010": {VIP_DISCORD_ROLE}, "800099": set()})
    pg.q(
        world.dal, "UPDATE community_members SET role = 'moderator' WHERE user_id = %s", [str(uid)]
    )
    await _platform_pass(world, pairing, {"800010": set(), "800099": set()})
    assert world.role_of(uid) == "moderator"


def test_ledger_db_check_refuses_privileged_grant(world: _World) -> None:
    pairing = world.pairing()
    uid = world.member("800010", "member")
    with pytest.raises(psycopg2.errors.CheckViolation):
        pg.q(
            world.dal,
            "INSERT INTO role_sync_community_grants "
            "(pairing_id, community_id, hub_user_id, granted_role) VALUES (%s, %s, %s, %s)",
            [pairing.id, world.cid, uid, "community-admin"],
        )
    world.dal.rollback()


# ----------------------------------------------------------------- outbound (Twitch -> Discord)


def _outbound_bindings(world: _World, pairing: Any) -> None:
    pairing_svc.create_binding(
        world.dal,
        world.cid,
        pairing.id,
        sync_scope="subscriber_tier",
        discord_role_id=TIER1_DISCORD_ROLE,
        subscriber_tier=1,
    )
    pairing_svc.create_binding(
        world.dal, world.cid, pairing.id, sync_scope="moderator", discord_role_id=MOD_DISCORD_ROLE
    )


async def _outbound_pass(world: _World, pairing: Any, twitch: Any, discord: Any) -> Any:
    async def _token(_cid: int) -> str:
        return "broadcaster-token"  # noqa: S105 - fake

    return await svc.reconcile_pairing(
        world.dal,
        pairing,
        get_broadcaster_user_token=_token,
        credential_resolver=_FakeCredentialResolver(),  # type: ignore[arg-type]
        twitch_client=twitch,
        make_discord_client=lambda token: discord,
    )


async def test_twitch_moderator_not_propagated_by_default(world: _World) -> None:
    pairing = world.pairing("twitch_to_discord")
    _outbound_bindings(world, pairing)
    world.member(TWITCH_MOD, "member", platform="twitch")
    pg.q(
        world.dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "SELECT hub_user_id, 'discord', %s FROM hub_user_identities WHERE platform_user_id = %s",
        [DISCORD_OF_TWITCH_MOD, TWITCH_MOD],
    )
    assert world.row(pairing.id).propagate_moderators is False
    twitch = _FakeTwitchClient(mods={TWITCH_MOD})
    discord = _FakeDiscordClient({DISCORD_OF_TWITCH_MOD: set()})
    result = await _outbound_pass(world, world.row(pairing.id), twitch, discord)
    assert result.error is None and result.roles_added == 0
    assert discord.add_calls == []
    assert "list_moderators" not in twitch.calls

    # Explicit per-pairing opt-in keeps the feature available.
    pairing_svc.update_pairing(world.dal, world.cid, pairing.id, propagate_moderators=True)
    result = await _outbound_pass(world, world.row(pairing.id), twitch, discord)
    assert result.roles_added == 1
    assert (DISCORD_OF_TWITCH_MOD, MOD_DISCORD_ROLE) in discord.add_calls


async def test_outbound_removes_only_its_own_discord_grants(world: _World) -> None:
    pairing = world.pairing("twitch_to_discord")
    _outbound_bindings(world, pairing)
    world.member("700010", "member", platform="twitch")
    pg.q(
        world.dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "SELECT hub_user_id, 'discord', '800010' FROM hub_user_identities "
        "WHERE platform_user_id = '700010'",
    )
    # Subscribed: sync adds the tier role and ledgers it.
    discord = _FakeDiscordClient({"800010": set(), "800020": {TIER1_DISCORD_ROLE}})
    await _outbound_pass(world, pairing, _FakeTwitchClient(tiers={"700010": 1}), discord)
    assert ("800010", TIER1_DISCORD_ROLE) in discord.add_calls
    # Sub lapses: the ledgered grant is removed; 800020's hand-given tier role is never touched.
    discord._member_roles["800010"] = {TIER1_DISCORD_ROLE}
    result = await _outbound_pass(world, pairing, _FakeTwitchClient(tiers={}), discord)
    assert result.roles_removed == 1
    assert discord.remove_calls == [("800010", TIER1_DISCORD_ROLE)]
    assert pg.scalar(world.dal, "SELECT count(*) FROM role_sync_discord_grants") == 0


async def test_outbound_skips_binding_whose_role_became_privileged(world: _World) -> None:
    pairing = world.pairing("twitch_to_discord")
    _outbound_bindings(world, pairing)
    world.member("700010", "member", platform="twitch")
    pg.q(
        world.dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "SELECT hub_user_id, 'discord', '800010' FROM hub_user_identities "
        "WHERE platform_user_id = '700010'",
    )
    discord = _FakeDiscordClient({"800010": set()})

    async def _roles(*, guild_id: str) -> list[Any]:
        return [
            svc.DiscordGuildRoleInfo(role_id=TIER1_DISCORD_ROLE, permissions=0x8, managed=False)
        ]

    discord.get_guild_roles = _roles  # type: ignore[attr-defined]
    result = await _outbound_pass(world, pairing, _FakeTwitchClient(tiers={"700010": 1}), discord)
    assert result.roles_added == 0 and discord.add_calls == []


async def test_outbound_privileged_role_refused_at_binding_time(world: _World) -> None:
    from services.discord_guild_roles import DiscordRole

    pairing = world.pairing("twitch_to_discord")

    async def _roles(dal: Any, community_id: int, *, guild_id: str | None = None) -> list[Any]:
        return [DiscordRole(id=MOD_DISCORD_ROLE, name="m", color=0, position=1, privileged=True)]

    with pytest.raises(ApiError) as excinfo:
        await pairing_svc.create_binding_checked(
            world.dal,
            world.cid,
            pairing.id,
            sync_scope="moderator",
            discord_role_id=MOD_DISCORD_ROLE,
            list_roles=_roles,
        )
    assert excinfo.value.code == "privileged_target_role"
    assert pairing_svc.list_bindings(world.dal, world.cid, pairing.id) == []


# ----------------------------------------------------------------- kill switch


async def test_kill_switch_still_honoured(world: _World, _flag_on: AsyncMock) -> None:
    _flag_on.return_value = False
    pairing = await _vip_pairing(world)
    uid = world.member(ATTACKER, "member")
    result = await _platform_pass(world, pairing, {ATTACKER: {VIP_DISCORD_ROLE}})
    assert result.community_roles_applied == 0
    assert world.role_of(uid) == "member"


def test_new_unverified_pairing_is_not_grandfathered(world: _World) -> None:
    """A pairing created after 0104 without the API proof is neither verified nor grandfathered."""
    pairing = world.pairing(verified=False)
    assert pairing.ownership_verified_at is None
    assert pairing.grandfathered is False  # only rows present at upgrade time are marked


# ----------------------------------------------------------------- bounded grandfathering


def _flags(*, disable_unverified: bool) -> AsyncMock:
    """Role-sync ON; the unverified-pairing kill switch as given."""

    async def _eval(key: str, **_kw: Any) -> bool:
        if key == svc.FLAG_DISABLE_UNVERIFIED_PAIRING_SYNC:
            return disable_unverified
        return True

    return AsyncMock(side_effect=_eval)


async def _batch(world: _World, members: dict[str, set[str]]) -> Any:
    client = _FakeDiscordClient(
        {},
        guild_members=[
            svc.DiscordGuildMember(user_id=u, role_ids=frozenset(r)) for u, r in members.items()
        ],
    )

    async def _token(_cid: int) -> str:
        return "t"  # noqa: S105 - fake

    return await svc.run_role_sync_reconcile_batch(
        world.dal,
        get_broadcaster_user_token=_token,
        credential_resolver=_FakeCredentialResolver(),  # type: ignore[arg-type]
        twitch_client=_FakeTwitchClient(),
        make_discord_client=lambda token: client,
    )


@pytest.mark.parametrize(
    ("verified", "grandfathered", "switch_on", "expect_synced"),
    [
        (False, True, False, True),  # pre-0104 pairing, switch unseen/OFF -> still syncs
        (False, True, True, False),  # switch ON -> grandfathered pairing skipped
        (False, False, False, False),  # post-0104 unverified pairing never syncs
        (True, False, True, True),  # verified pairing unaffected by the switch
    ],
)
async def test_batch_gates_unverified_pairings(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
    grandfathered: bool,
    switch_on: bool,
    expect_synced: bool,
) -> None:
    monkeypatch.setattr(svc, "feature_enabled", _flags(disable_unverified=switch_on))
    pairing = world.pairing(verified=verified, grandfathered=grandfathered)
    pairing_svc.create_binding(
        world.dal,
        world.cid,
        pairing.id,
        sync_scope="community_role",
        discord_role_id=VIP_DISCORD_ROLE,
        community_role="vip",
    )
    uid = world.member(ATTACKER, "member")
    summary = await _batch(world, {ATTACKER: {VIP_DISCORD_ROLE}})
    assert summary.pairings_examined == 1
    assert summary.pairings_skipped_unverified == (0 if expect_synced else 1)
    assert world.role_of(uid) == ("vip" if expect_synced else "member")


async def test_inbound_push_refuses_grandfathered_pairing_when_switch_on(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "feature_enabled", _flags(disable_unverified=True))
    pairing = world.pairing(verified=False, grandfathered=True)
    pairing_svc.create_binding(
        world.dal,
        world.cid,
        pairing.id,
        sync_scope="community_role",
        discord_role_id=VIP_DISCORD_ROLE,
        community_role="vip",
    )
    uid = world.member(ATTACKER, "member")
    event = inbound.InboundRoleEvent(
        event_type="guild.member.update",
        tenant_id=world.tenant_id,
        community_id=world.cid,
        guild_id=GUILD_ID,
        user_id=ATTACKER,
        role_id=None,
        role_ids=frozenset({VIP_DISCORD_ROLE}),
    )

    class _Confirm:
        async def member_roles(self, dal: Any, **kw: Any) -> set[str]:
            return {VIP_DISCORD_ROLE}

    out = await inbound.apply_inbound_event(world.dal, event, _Confirm())  # type: ignore[arg-type]
    assert out.outcome == "pairing_unverified"
    assert world.role_of(uid) == "member"


# ----------------------------------------------------------------- stale revocation + backfill


def _sub_world(world: _World, **pairing_kw: Any) -> Any:
    pairing = world.pairing("twitch_to_discord", **pairing_kw)
    _outbound_bindings(world, pairing)
    world.member("700010", "member", platform="twitch")
    pg.q(
        world.dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "SELECT hub_user_id, 'discord', '800010' FROM hub_user_identities "
        "WHERE platform_user_id = '700010'",
    )
    return pairing


class _FailingRemove(_FakeDiscordClient):
    """Discord refuses the removal (e.g. hierarchy / transient)."""

    async def remove_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        self.remove_calls.append((user_id, role_id))
        return False


def _ledger_count(world: _World) -> int:
    return int(pg.scalar(world.dal, "SELECT count(*) FROM role_sync_discord_grants"))


async def test_failed_removal_keeps_ledger_row_for_retry(world: _World) -> None:
    pairing = _sub_world(world)
    svc._record_discord_grant(world.dal, pairing.id, TIER1_DISCORD_ROLE, "800010")
    failing = _FailingRemove({"800010": {TIER1_DISCORD_ROLE}})
    first = await _outbound_pass(world, world.row(pairing.id), _FakeTwitchClient(), failing)
    assert first.roles_removed == 0 and _ledger_count(world) == 1  # kept -> retried next pass
    ok = _FakeDiscordClient({"800010": {TIER1_DISCORD_ROLE}})
    second = await _outbound_pass(world, world.row(pairing.id), _FakeTwitchClient(), ok)
    assert second.roles_removed == 1 and _ledger_count(world) == 0


async def test_confirmed_absent_role_drops_ledger_row(world: _World) -> None:
    pairing = _sub_world(world)
    svc._record_discord_grant(world.dal, pairing.id, TIER1_DISCORD_ROLE, "800010")
    discord = _FakeDiscordClient({"800010": set()})  # someone already removed it
    result = await _outbound_pass(world, world.row(pairing.id), _FakeTwitchClient(), discord)
    assert result.roles_removed == 0 and discord.remove_calls == []
    assert _ledger_count(world) == 0


async def test_grandfathered_pairing_backfills_ledger_once_then_removes(world: _World) -> None:
    """Pre-0104 adds had no ledger: first pass backfills, so a lapsed sub's role is removed."""
    pairing = _sub_world(world, verified=False, grandfathered=True)
    discord = _FakeDiscordClient({"800010": {TIER1_DISCORD_ROLE}, "800030": {"999"}})
    result = await _outbound_pass(world, world.row(pairing.id), _FakeTwitchClient(), discord)
    assert result.roles_removed == 1  # backfilled grant, sub lapsed -> removed
    assert discord.remove_calls == [("800010", TIER1_DISCORD_ROLE)]
    assert world.row(pairing.id).discord_grants_backfilled_at is not None
    # A later hand-assigned role is NOT re-backfilled (one-time), so it is never removed.
    discord._member_roles["800010"] = {TIER1_DISCORD_ROLE}
    again = await _outbound_pass(world, world.row(pairing.id), _FakeTwitchClient(), discord)
    assert again.roles_removed == 0


async def test_verified_new_pairing_never_backfills(world: _World) -> None:
    pairing = _sub_world(world)
    discord = _FakeDiscordClient({"800010": {TIER1_DISCORD_ROLE}})
    result = await _outbound_pass(world, world.row(pairing.id), _FakeTwitchClient(), discord)
    assert result.roles_removed == 0 and _ledger_count(world) == 0


# ----------------------------------------------------------------- purged-binding remediation


def _purged(world: _World, pairing: Any, role: str) -> int:
    return int(
        pg.scalar(
            world.dal,
            "INSERT INTO role_sync_purged_bindings "
            "(pairing_id, community_id, discord_role_id, community_role) "
            "VALUES (%s, %s, '5', %s) RETURNING id",
            [pairing.id, world.cid, role],
        )
    )


def test_remediation_queues_by_default_and_demotes_with_apply(world: _World) -> None:
    pairing = world.pairing()
    _purged(world, pairing, "community-admin")
    promoted = world.member("800050", "community-admin")
    world.member("800051", "moderator")  # different role than the purged binding -> not a hit
    unlinked = int(
        pg.scalar(world.dal, "INSERT INTO hub_users (username) VALUES ('x') RETURNING id")
    )
    pg.q(
        world.dal,
        "INSERT INTO community_members (community_id, user_id, role, is_active) "
        "VALUES (%s, %s, 'community-admin', true)",
        [world.cid, str(unlinked)],
    )  # hand-made admin with no Discord link -> not attributable to sync

    review = remediate_purged_bindings(world.dal)
    assert (review.purged_bindings_examined, review.candidates, review.newly_queued) == (1, 1, 1)
    assert review.demoted == 0 and world.role_of(promoted) == "community-admin"
    again = remediate_purged_bindings(world.dal)
    assert again.newly_queued == 0  # idempotent

    applied = remediate_purged_bindings(world.dal, apply=True)
    assert applied.demoted == 1
    assert world.role_of(promoted) == "member"
    assert world.role_of(unlinked) == "community-admin"
    assert pg.scalar(world.dal, "SELECT status FROM role_sync_remediation_queue") == STATUS_DEMOTED


def test_remediation_with_nothing_purged_reports_zero(world: _World) -> None:
    report = remediate_purged_bindings(world.dal, apply=True)
    assert report.purged_bindings_examined == 0 and report.demoted == 0
