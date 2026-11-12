"""Cross-guild Discord role mirror: mapping validation, reconcile pass, routes (migration 0099).

Discord is faked through the `DiscordRoleTargetClient` seam (no network); the 429 handling test
drives the real `HttpDiscordRoleTargetClient` over `httpx.MockTransport`.
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.guild_pairing import guild_pairing_bp
from services import discord_role_mirror as mirror
from services import guild_pairing as pairing_svc_mod
from services import role_sync_service as svc
from services.credential_resolver import PlatformCredentials, TransportUnavailable
from services.discord_guild_roles import DiscordRole, is_privileged_role
from services.errors import ApiError
from services.guild_pairing import (
    create_binding,
    create_pairing,
    delete_pairing,
    list_pairings,
    update_pairing,
)

G_A = "111111111111111111"  # gitleaks:allow - fake snowflakes
G_B = "222222222222222222"
G_C = "333333333333333333"
R_SRC = "900000000000000001"
R_DST = "900000000000000002"
R_OTHER = "900000000000000003"
U1, U2, U3 = "700000000000000001", "700000000000000002", "700000000000000003"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """PostHog flag ON for the reconcile pass; individual tests override."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


class _FakeGuilds:
    """In-memory two-guild Discord: guild -> user -> role-id set; records writes."""

    def __init__(self, guilds: dict[str, dict[str, set[str]]], *, complete: bool = True) -> None:
        self.guilds = guilds
        self.complete = complete
        #: guild -> role -> (permissions, managed) overrides for the privilege re-check
        self.role_info: dict[str, dict[str, tuple[int, bool]]] = {}
        self.adds: list[tuple[str, str, str]] = []
        self.removes: list[tuple[str, str, str]] = []

    async def get_guild_roles(self, *, guild_id: str) -> list[svc.DiscordGuildRoleInfo]:
        overrides = self.role_info.get(guild_id, {})
        out = [
            svc.DiscordGuildRoleInfo(role_id=r, permissions=p, managed=m)
            for r, (p, m) in overrides.items()
        ]
        out += [
            svc.DiscordGuildRoleInfo(role_id=r, permissions=0, managed=False)
            for r in _ALL_ROLES.get(guild_id, [])
            if r not in overrides
        ]
        return out

    async def get_member_role_ids(self, *, guild_id: str, user_id: str) -> set[str] | None:
        return self.guilds[guild_id].get(user_id)

    async def add_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        self.adds.append((guild_id, user_id, role_id))
        self.guilds[guild_id][user_id].add(role_id)
        return True

    async def remove_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        self.removes.append((guild_id, user_id, role_id))
        self.guilds[guild_id][user_id].discard(role_id)
        return True

    async def list_guild_members(self, *, guild_id: str) -> list[svc.DiscordGuildMember]:
        return (await self.list_guild_members_checked(guild_id=guild_id)).members

    async def list_guild_members_checked(self, *, guild_id: str) -> svc.GuildMemberSnapshot:
        members = [
            svc.DiscordGuildMember(user_id=u, role_ids=frozenset(r))
            for u, r in self.guilds[guild_id].items()
        ]
        return svc.GuildMemberSnapshot(members=members, complete=self.complete)


class _Creds:
    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        return PlatformCredentials(
            tenant_id=tenant_id,
            platform=platform,
            payload={"bot_token": "fake-bot-token"},  # noqa: S106 - test fixture
            source="tenant",
        )


class _NoCreds:
    async def resolve(self, *a: Any, **k: Any) -> Any:
        raise TransportUnavailable("no creds")


def _lister(roles_by_guild: dict[str, list[str]]) -> Any:
    async def _list(dal: Any, community_id: int, guild_id: str) -> list[DiscordRole]:
        return [DiscordRole(id=r, name=r, color=0, position=1) for r in roles_by_guild[guild_id]]

    return _list


_ALL_ROLES = {
    G_A: [R_SRC, R_DST, R_OTHER],
    G_B: [R_SRC, R_DST, R_OTHER],
    G_C: [R_SRC, R_DST],
}


def _pair(dal: Any, community_id: int, *guilds: str, enabled: bool = True) -> None:
    for g in guilds:
        p = create_pairing(
            dal,
            community_id,
            discord_guild_id=g,
            direction="bidirectional",
            role_name_prefix="[T]",
            actor_user_id=None,
        )
        if enabled:
            update_pairing(dal, community_id, p.id, sync_enabled=True)


async def _map(dal: Any, community_id: int, sg: str, sr: str, tg: str, tr: str) -> Any:
    return await mirror.create_mapping(
        dal,
        community_id,
        source_guild_id=sg,
        source_role_id=sr,
        target_guild_id=tg,
        target_role_id=tr,
        list_roles=_lister(_ALL_ROLES),
    )


async def _run(dal: Any, fake: _FakeGuilds, creds: Any = None) -> mirror.MirrorSummary:
    return await mirror.reconcile_role_mirrors(
        dal, credential_resolver=creds or _Creds(), make_discord_client=lambda _t: fake
    )


class TestMappingValidation:
    async def test_create_and_list(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        m = await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert (m.source_guild_id, m.target_guild_id) == (G_A, G_B)
        assert [x.id for x in mirror.list_mappings(dal, cid)] == [m.id]

    async def test_guild_not_paired_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert exc.value.code == "DISCORD_PAIRING_NOT_FOUND"

    async def test_guild_paired_to_other_community_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, tenant_id, _g = guild_pairing_db
        other = dal.communities.insert(name="other-community", tenant_id=tenant_id)
        dal.commit()
        _pair(dal, cid, G_A)
        _pair(dal, other, G_B)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert exc.value.status_code == 404

    async def test_different_tenant_guild_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        other_tenant = dal.tenants.insert(slug="other-corp", is_active=True, is_global=False)
        other = dal.communities.insert(name="oc", tenant_id=other_tenant)
        dal.commit()
        _pair(dal, cid, G_A)
        _pair(dal, other, G_B)
        with pytest.raises(ApiError):
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)

    async def test_same_guild_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_A, R_DST)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("bad", ["", "abc", "12 3"])
    async def test_bad_snowflake_rejected(self, guild_pairing_db: Any, bad: str) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, bad, G_B, R_DST)
        assert exc.value.status_code == 400

    async def test_missing_role_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        with pytest.raises(ApiError) as exc:
            await mirror.create_mapping(
                dal,
                cid,
                source_guild_id=G_A,
                source_role_id=R_SRC,
                target_guild_id=G_B,
                target_role_id="999999999999999999",
                list_roles=_lister(_ALL_ROLES),
            )
        assert exc.value.status_code == 404

    async def test_duplicate_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert exc.value.status_code == 409

    async def test_direct_cycle_rejected(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_B, R_DST, G_A, R_SRC)
        assert exc.value.status_code == 409

    async def test_transitive_cycle_rejected_but_other_role_pair_ok(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B, G_C)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        await _map(dal, cid, G_B, R_DST, G_C, R_DST)
        with pytest.raises(ApiError):
            await _map(dal, cid, G_C, R_DST, G_A, R_SRC)
        # A different role pair in the reverse direction is NOT a cycle.
        await _map(dal, cid, G_B, R_OTHER, G_A, R_OTHER)

    def test_would_create_cycle_pure(self) -> None:
        a, b, c = ("g1", "r"), ("g2", "r"), ("g3", "r")
        assert mirror.would_create_cycle([(a, b)], (b, a))
        assert mirror.would_create_cycle([(a, b), (b, c)], (c, a))
        assert not mirror.would_create_cycle([(a, b)], (a, c))

    async def test_delete_mapping(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        m = await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert mirror.delete_mapping(dal, cid, m.id) is True
        assert mirror.list_mappings(dal, cid) == []
        assert mirror.delete_mapping(dal, cid, m.id) is False


def _lister_priv(perm_by_role: dict[str, tuple[int, bool]]) -> Any:
    async def _list(dal: Any, community_id: int, guild_id: str) -> list[DiscordRole]:
        out = []
        for r in _ALL_ROLES[guild_id]:
            perms, managed = perm_by_role.get(r, (0, False))
            out.append(
                DiscordRole(
                    id=r,
                    name=r,
                    color=0,
                    position=1,
                    permissions=perms,
                    managed=managed,
                    privileged=is_privileged_role(perms, managed),
                )
            )
        return out

    return _list


class TestPrivilegedAndEveryone:
    @pytest.mark.parametrize(
        ("perms", "managed"),
        [
            (0x8, False),
            (0x20, False),
            (0x10000000, False),
            (0x10, False),
            (0x20000000, False),
            (0x4, False),
            (0x2, False),
            (0x10000000000, False),
            (0x20000, False),
            (0, True),
        ],
    )
    async def test_privileged_or_managed_target_rejected_422(
        self, guild_pairing_db: Any, perms: int, managed: bool
    ) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        with pytest.raises(ApiError) as exc:
            await mirror.create_mapping(
                dal,
                cid,
                source_guild_id=G_A,
                source_role_id=R_SRC,
                target_guild_id=G_B,
                target_role_id=R_DST,
                list_roles=_lister_priv({R_DST: (perms, managed)}),
            )
        assert exc.value.status_code == 422 and exc.value.code == "privileged_target_role"
        assert mirror.list_mappings(dal, cid) == []

    async def test_privileged_source_role_is_allowed(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        m = await mirror.create_mapping(
            dal,
            cid,
            source_guild_id=G_A,
            source_role_id=R_SRC,
            target_guild_id=G_B,
            target_role_id=R_DST,
            list_roles=_lister_priv({R_SRC: (0x8, False)}),
        )
        assert m.id

    @pytest.mark.parametrize("which", ["source", "target"])
    async def test_everyone_rejected(self, guild_pairing_db: Any, which: str) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        args = (G_A, G_A, G_B, R_DST) if which == "source" else (G_A, R_SRC, G_B, G_B)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, *args)
        assert exc.value.status_code == 400

    async def test_role_becomes_privileged_after_mapping_sync_refuses(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        fake.role_info[G_B] = {R_DST: (0x8, False)}  # became ADMINISTRATOR after mapping
        summary = await _run(dal, fake)
        assert fake.adds == [] and fake.removes == []
        assert summary.mappings_failed == 1
        assert mirror.list_mappings(dal, cid)[0].last_error == "privileged_target_role"

    async def test_managed_target_refused_at_sync(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        fake.role_info[G_B] = {R_DST: (0, True)}
        await _run(dal, fake)
        assert fake.adds == []

    async def test_deleted_target_role_marked_missing(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        _ALL_ROLES[G_B].remove(R_DST)
        try:
            await _run(dal, fake)
        finally:
            _ALL_ROLES[G_B].insert(1, R_DST)
        assert fake.adds == []
        assert mirror.list_mappings(dal, cid)[0].last_error == "target_role_missing"

    async def test_route_returns_422_for_privileged_target(
        self, app: Quart, auth_headers: Any, guild_pairing_db: Any, monkeypatch: Any
    ) -> None:
        import blueprints.v1.guild_pairing as bp

        dal, cid, *_ = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        monkeypatch.setattr(bp, "_list_roles_for_mirror", _lister_priv({R_DST: (0x8, False)}))
        r = await app.test_client().post(
            f"/api/v1/communities/{cid}/discord/role-mirrors",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {
                    "source_guild_id": G_A,
                    "source_role_id": R_SRC,
                    "target_guild_id": G_B,
                    "target_role_id": R_DST,
                }
            ),
        )
        assert r.status_code == 422
        assert (await r.get_json())["error"]["code"] == "privileged_target_role"


class TestPairingLifecycleAndBindings:
    async def test_disabled_pairing_rejected_at_create(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, enabled=True)
        _pair(dal, cid, G_B, enabled=False)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert exc.value.status_code == 409 and exc.value.code == "pairing_not_enabled"

    async def test_unpaired_guild_mapping_skipped_fail_closed(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        pairing_b = next(p for p in list_pairings(dal, cid) if p.discord_guild_id == G_B)
        update_pairing(dal, cid, pairing_b.id, sync_enabled=False)  # disabled since mapping
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        summary = await _run(dal, fake)
        assert fake.adds == [] and fake.removes == [] and summary.mappings_failed == 1
        assert mirror.list_mappings(dal, cid)[0].last_error == "not_paired"

    async def test_unpair_deletes_mappings_and_grants_without_touching_roles(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        await _run(dal, fake)
        assert dal(dal.discord_role_mirror_grants.id > 0).count() == 1
        pairing_b = next(p for p in list_pairings(dal, cid) if p.discord_guild_id == G_B)
        assert delete_pairing(dal, cid, pairing_b.id) is True
        assert mirror.list_mappings(dal, cid) == []
        assert dal(dal.discord_role_mirror_grants.id > 0).count() == 0
        assert fake.removes == [] and R_DST in fake.guilds[G_B][U1]

    async def test_mapping_rejects_role_used_by_binding(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        pa = next(p for p in list_pairings(dal, cid) if p.discord_guild_id == G_A)
        create_binding(dal, cid, pa.id, sync_scope="moderator", discord_role_id=R_SRC)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert exc.value.status_code == 409
        pb = next(p for p in list_pairings(dal, cid) if p.discord_guild_id == G_B)
        create_binding(dal, cid, pb.id, sync_scope="moderator", discord_role_id=R_DST)
        with pytest.raises(ApiError):
            await _map(dal, cid, G_A, R_OTHER, G_B, R_DST)

    @pytest.mark.parametrize("side", ["source", "target"])
    async def test_binding_rejects_role_used_by_mirror(
        self, guild_pairing_db: Any, side: str
    ) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        guild, role = (G_A, R_SRC) if side == "source" else (G_B, R_DST)
        p = next(x for x in list_pairings(dal, cid) if x.discord_guild_id == guild)
        with pytest.raises(ApiError) as exc:
            create_binding(dal, cid, p.id, sync_scope="moderator", discord_role_id=role)
        assert exc.value.status_code == 409
        # an unrelated role in the same guild is still bindable
        create_binding(dal, cid, p.id, sync_scope="moderator", discord_role_id=R_OTHER)


class TestAllowlistAndCrossCommunity:
    @pytest.mark.parametrize(
        ("perms", "privileged"),
        [
            (0, False),
            (0x400 | 0x800 | 0x10000 | 0x40, False),  # view/send/history/reactions
            (1 << 31 | 1 << 35 | 1 << 38, False),  # app commands, threads
            (0x1, True),  # CREATE_INSTANT_INVITE is not on the allowlist
            (1 << 13, True),  # MANAGE_MESSAGES
            (1 << 40, True),  # unknown/future bit
            (0x400 | 0x8, True),
        ],
    )
    def test_allowlist(self, perms: int, privileged: bool) -> None:
        assert is_privileged_role(perms, False) is privileged
        assert is_privileged_role(0, True) is True  # managed always refused

    async def _other_community(self, dal: Any, tenant_id: int, *guilds: str) -> int:
        cid2 = dal.communities.insert(name="second-community", tenant_id=tenant_id)
        dal.commit()
        _pair(dal, cid2, *guilds)
        return int(cid2)

    async def test_mapping_rejects_role_bound_via_other_community_pairing(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, tenant_id, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        cid2 = await self._other_community(dal, tenant_id, G_A)
        p2 = next(p for p in list_pairings(dal, cid2) if p.discord_guild_id == G_A)
        create_binding(dal, cid2, p2.id, sync_scope="moderator", discord_role_id=R_SRC)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        assert exc.value.status_code == 409

    async def test_binding_rejects_role_mirrored_by_other_community(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, tenant_id, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        cid2 = await self._other_community(dal, tenant_id, G_B)
        p2 = next(p for p in list_pairings(dal, cid2) if p.discord_guild_id == G_B)
        with pytest.raises(ApiError) as exc:
            create_binding(dal, cid2, p2.id, sync_scope="moderator", discord_role_id=R_DST)
        assert exc.value.status_code == 409

    async def _foreign_community(self, dal: Any, *guilds: str) -> int:
        t2 = dal.tenants.insert(slug="foreign-corp", is_active=True, is_global=False)
        cid2 = dal.communities.insert(name="foreign-community", tenant_id=t2)
        dal.commit()
        _pair(dal, cid2, *guilds)
        return int(cid2)

    @staticmethod
    def _assert_generic(err: ApiError, *leaks: Any) -> None:
        assert err.status_code == 409
        assert str(err.message) == pairing_svc_mod.ROLE_UNAVAILABLE_MESSAGE
        for leak in leaks:
            assert str(leak) not in str(err.message)

    async def test_cross_tenant_binding_conflict_is_generic(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        cid2 = await self._foreign_community(dal, G_A)
        p2 = next(p for p in list_pairings(dal, cid2) if p.discord_guild_id == G_A)
        create_binding(dal, cid2, p2.id, sync_scope="moderator", discord_role_id=R_SRC)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        self._assert_generic(exc.value, cid2, p2.id, "foreign", "binding")

    async def test_cross_tenant_target_conflict_is_generic(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        cid2 = await self._foreign_community(dal, G_B, G_C)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid2, G_C, R_SRC, G_B, R_DST)
        self._assert_generic(exc.value, cid, "mirror mapping")
        assert mirror.list_mappings(dal, cid2) == []

    async def test_cross_tenant_binding_vs_mirror_is_generic(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        cid2 = await self._foreign_community(dal, G_B)
        p2 = next(p for p in list_pairings(dal, cid2) if p.discord_guild_id == G_B)
        with pytest.raises(ApiError) as exc:
            create_binding(dal, cid2, p2.id, sync_scope="moderator", discord_role_id=R_DST)
        self._assert_generic(exc.value, cid, R_DST)
        assert exc.value.details is None

    async def test_target_role_cannot_be_target_of_another_community_mapping(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, tenant_id, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        cid2 = await self._other_community(dal, tenant_id, G_B, G_C)
        with pytest.raises(ApiError) as exc:
            await _map(dal, cid2, G_C, R_SRC, G_B, R_DST)
        assert exc.value.status_code == 409


class TestReconcile:
    async def _setup(self, dal: Any, cid: int) -> Any:
        _pair(dal, cid, G_A, G_B)
        return await _map(dal, cid, G_A, R_SRC, G_B, R_DST)

    async def test_adds_target_role_and_skips_non_member(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        m = await self._setup(dal, cid)
        fake = _FakeGuilds(
            {G_A: {U1: {R_SRC}, U2: {R_SRC}, U3: set()}, G_B: {U1: set(), U3: set()}}
        )
        summary = await _run(dal, fake)
        assert fake.adds == [(G_B, U1, R_DST)]  # U2 not in B -> skipped; U3 lacks src role
        assert (summary.roles_added, summary.skipped_not_member, summary.mappings_failed) == (
            1,
            1,
            0,
        )
        row = mirror.list_mappings(dal, cid)[0]
        assert (row.id, row.last_roles_added, row.last_skipped_not_member) == (m.id, 1, 1)
        assert row.last_error is None and row.last_synced_at is not None

    async def test_idempotent_second_pass(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        await _run(dal, fake)
        await _run(dal, fake)
        assert len(fake.adds) == 1 and fake.removes == []

    async def test_removes_only_mirror_granted_role(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        # U1: mirror will grant. U3: already holds target role manually, also holds source.
        fake = _FakeGuilds({G_A: {U1: {R_SRC}, U3: {R_SRC}}, G_B: {U1: set(), U3: {R_DST}}})
        await _run(dal, fake)
        assert fake.adds == [(G_B, U1, R_DST)]
        # Both lose the source role.
        fake.guilds[G_A][U1].discard(R_SRC)
        fake.guilds[G_A][U3].discard(R_SRC)
        summary = await _run(dal, fake)
        assert fake.removes == [(G_B, U1, R_DST)]  # U3's manual role survives
        assert R_DST in fake.guilds[G_B][U3]
        assert summary.roles_removed == 1
        # Ledger is clean now -> a further pass is a no-op.
        await _run(dal, fake)
        assert len(fake.removes) == 1

    async def test_no_removal_when_member_list_incomplete(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        await _run(dal, fake)
        fake.guilds[G_A][U1].discard(R_SRC)
        fake.complete = False
        await _run(dal, fake)
        assert fake.removes == []  # unprovable read => demote nobody

    async def test_shared_target_role_kept_while_other_mapping_holds_it(
        self, guild_pairing_db: Any
    ) -> None:
        dal, cid, _t, _g = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        await _map(dal, cid, G_A, R_SRC, G_B, R_DST)
        # create_mapping now forbids a shared target; insert directly to keep the defensive
        # "another mapping still wants it" removal guard covered.
        dal.discord_role_mirror_mappings.insert(
            community_id=cid,
            source_guild_id=G_A,
            source_role_id=R_OTHER,
            target_guild_id=G_B,
            target_role_id=R_DST,
        )
        dal.commit()
        fake = _FakeGuilds({G_A: {U1: {R_SRC, R_OTHER}}, G_B: {U1: set()}})
        await _run(dal, fake)
        assert len(fake.adds) == 1  # second mapping sees the role the first just granted
        fake.guilds[G_A][U1].discard(R_SRC)  # still held via the OTHER mapping's source role
        await _run(dal, fake)
        assert fake.removes == [] and R_DST in fake.guilds[G_B][U1]
        fake.guilds[G_A][U1].discard(R_OTHER)
        await _run(dal, fake)
        assert fake.removes == [(G_B, U1, R_DST)]

    async def test_flag_off_does_nothing(self, guild_pairing_db: Any, _flag_on: AsyncMock) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        _flag_on.return_value = False
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        await _run(dal, fake)
        assert fake.adds == []

    async def test_credential_failure_marks_error(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        summary = await _run(dal, _FakeGuilds({G_A: {}, G_B: {}}), creds=_NoCreds())
        assert summary.mappings_failed == 1
        assert mirror.list_mappings(dal, cid)[0].last_error == "TransportUnavailable"

    async def test_discord_error_is_isolated_and_recorded(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})
        fake.add_role = AsyncMock(side_effect=svc.DiscordSyncError("403"))  # type: ignore[method-assign]
        summary = await _run(dal, fake)
        assert summary.mappings_failed == 1
        assert mirror.list_mappings(dal, cid)[0].last_error == "DiscordSyncError"

    async def test_batch_runs_mirror_pass(self, guild_pairing_db: Any) -> None:
        dal, cid, _t, _g = guild_pairing_db
        await self._setup(dal, cid)
        fake = _FakeGuilds({G_A: {U1: {R_SRC}}, G_B: {U1: set()}})

        async def _no_token(_c: int) -> str | None:
            return None

        summary = await svc.run_role_sync_reconcile_batch(
            dal,
            get_broadcaster_user_token=_no_token,
            credential_resolver=_Creds(),
            make_discord_client=lambda _t: fake,
        )
        assert summary.mirror_roles_added == 1 and summary.mirror_mappings_examined == 1


class TestRateLimit:
    async def test_429_retry_after_honoured_then_succeeds(self) -> None:
        calls = {"n": 0}
        slept: list[float] = []

        def handler(_r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(204)

        async def _sleep(s: float) -> None:
            slept.append(s)

        client = svc.HttpDiscordRoleTargetClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            bot_token="t",  # noqa: S106 - test fixture
            api_base="https://t.test",
            rate_limit_retries=2,
            sleep=_sleep,
        )
        assert await client.add_role(guild_id="g", user_id="u", role_id="r") is True
        assert slept == [2.0] and calls["n"] == 2

    async def test_429_exhausted_raises_and_retry_after_capped(self) -> None:
        slept: list[float] = []

        async def _sleep(s: float) -> None:
            slept.append(s)

        client = svc.HttpDiscordRoleTargetClient(
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(429, headers={"Retry-After": "9999"})
                )
            ),
            bot_token="t",  # noqa: S106 - test fixture
            api_base="https://t.test",
            rate_limit_retries=2,
            sleep=_sleep,
        )
        with pytest.raises(svc.DiscordSyncError, match="429"):
            await client.remove_role(guild_id="g", user_id="u", role_id="r")
        assert slept == [10.0, 10.0]


@pytest.fixture
def app(guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch) -> Quart:
    import blueprints.v1.guild_pairing as bp

    dal, *_ = guild_pairing_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(guild_pairing_bp)
    quart_app.config["dal"] = dal
    monkeypatch.setattr(bp, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(bp, "_list_roles_for_mirror", _lister(_ALL_ROLES))
    return quart_app


class TestRoutes:
    def _body(self, **o: str) -> str:
        b = {
            "source_guild_id": G_A,
            "source_role_id": R_SRC,
            "target_guild_id": G_B,
            "target_role_id": R_DST,
        }
        b.update(o)
        return json_module.dumps(b)

    async def test_member_forbidden(
        self, app: Quart, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _d, cid, *_ = guild_pairing_db
        c = app.test_client()
        url = f"/api/v1/communities/{cid}/discord/role-mirrors"
        assert (await c.get(url, headers=auth_headers(scope="profile:read"))).status_code == 403
        r = await c.post(
            url,
            headers={
                **auth_headers(scope="community.guild_pairing:read"),
                "Content-Type": "application/json",
            },
            data=self._body(),
        )
        assert r.status_code == 403
        assert (
            await c.delete(f"{url}/1", headers=auth_headers(scope="community.guild_pairing:read"))
        ).status_code == 403

    async def test_create_list_delete_roundtrip(
        self, app: Quart, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, cid, *_ = guild_pairing_db
        _pair(dal, cid, G_A, G_B)
        c = app.test_client()
        url = f"/api/v1/communities/{cid}/discord/role-mirrors"
        w = {
            **auth_headers(scope="community.guild_pairing:write"),
            "Content-Type": "application/json",
        }
        r = await c.post(url, headers=w, data=self._body())
        assert r.status_code == 201
        mapping = (await r.get_json())["mapping"]
        assert set(mapping) == {
            "id",
            "community_id",
            "source_guild_id",
            "source_role_id",
            "target_guild_id",
            "target_role_id",
            "last_synced_at",
            "last_roles_added",
            "last_roles_removed",
            "last_skipped_not_member",
            "last_error",
            "created_at",
        }
        lst = await c.get(url, headers=auth_headers(scope="community.guild_pairing:read"))
        assert [m["id"] for m in (await lst.get_json())["mappings"]] == [mapping["id"]]
        rev = await c.post(
            url,
            headers=w,
            data=self._body(
                source_guild_id=G_B, source_role_id=R_DST, target_guild_id=G_A, target_role_id=R_SRC
            ),
        )
        assert rev.status_code == 409
        d = await c.delete(f"{url}/{mapping['id']}", headers=w)
        assert d.status_code == 204
        assert (await c.delete(f"{url}/{mapping['id']}", headers=w)).status_code == 404

    async def test_cross_tenant_community_404(
        self, app: Quart, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, *_ = guild_pairing_db
        t2 = dal.tenants.insert(slug="other-corp", is_active=True, is_global=False)
        c2 = dal.communities.insert(name="oc", tenant_id=t2)
        dal.commit()
        r = await app.test_client().get(
            f"/api/v1/communities/{c2}/discord/role-mirrors",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert r.status_code == 404
