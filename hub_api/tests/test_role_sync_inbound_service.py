"""`services/role_sync_inbound_service.py` -- inbound Discord role push reconciler.

Real sqlite DB via `guild_pairing_db` (same real-pydal fixture as `test_role_sync_service.py`);
the Valkey client is a small in-memory fake of the four stream calls the worker makes.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import redis.exceptions

from services import role_sync_inbound_service as inbound
from services import role_sync_service as base
from services.guild_pairing import create_binding, create_pairing, update_pairing
from tests.test_role_sync_service import (
    _COMMUNITY_ADMIN_ROLE,
    _DISCORD_GUILD_ID,
    _DISCORD_PLATFORM_USER,
    _MEMBER_ROLE_DISCORD,
    _TIER1_ROLE,
    _add_community_member,
    _add_community_role,
    _get_member_role,
    _link_identity,
    _make_platform_pairing,
)

_OTHER_GUILD = "999999999999999901"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(base, "feature_enabled", stub)
    return stub


def _member(
    tenant_id: int,
    community_id: int,
    roles: list[str],
    *,
    guild: str = _DISCORD_GUILD_ID,
    user: str = _DISCORD_PLATFORM_USER,
) -> inbound.InboundRoleEvent:
    return inbound.InboundRoleEvent(
        tenant_id=tenant_id,
        community_id=community_id,
        event_type="guild.member.update",
        guild_id=guild,
        user_id=user,
        role_id=None,
        role_ids=frozenset(roles),
    )


def _seed(dal: Any, community_id: int, role: str = "member", target: str = "vip") -> Any:
    """Pairing whose Discord member role maps to `target`; hub user 1 holds `role`."""
    pairing = _make_platform_pairing(dal, community_id, target=target)
    _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER)
    _add_community_member(dal, community_id=community_id, hub_user_id=1, role=role)
    return pairing


class _Confirm:
    """Fake Discord confirmation: returns `roles` (or raises / None) and records the calls."""

    def __init__(self, roles: Any, *, raises: Exception | None = None) -> None:
        self.roles, self.raises, self.calls = roles, raises, []

    async def member_roles(self, dal: Any, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.roles


async def _apply(dal: Any, ev: Any, confirm: Any = None) -> Any:
    """Apply with a Discord that agrees with the payload unless a confirm is given."""
    return await inbound.apply_inbound_event(dal, ev, confirm or _Confirm(set(ev.role_ids)))


class TestApply:
    async def test_inbound_change_is_applied_then_idempotent(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        ev = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD])
        first = await _apply(dal, ev)
        again = await _apply(dal, ev)  # redelivery
        assert first == inbound.InboundOutcome("applied", "vip")
        assert again.outcome == "unchanged"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "vip"

    async def test_highest_priority_mapped_role_wins(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        pairing = _seed(dal, community_id, target="member")
        create_binding(
            dal,
            community_id,
            pairing.id,
            sync_scope="community_role",
            discord_role_id=_COMMUNITY_ADMIN_ROLE,
            community_role="vip",
        )
        _add_community_role(dal, community_id=community_id, name="member", priority=0)
        _add_community_role(dal, community_id=community_id, name="vip", priority=10)
        out = await _apply(
            dal, _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD, _COMMUNITY_ADMIN_ROLE])
        )
        assert out.applied_role == "vip"

    async def test_self_caused_outbound_echo_is_ignored(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        pairing = _seed(dal, community_id)
        create_binding(
            dal,
            community_id,
            pairing.id,
            sync_scope="subscriber_tier",
            discord_role_id=_TIER1_ROLE,
            subscriber_tier=1,
        )
        out = await _apply(dal, _member(tenant_id, community_id, [_TIER1_ROLE]))
        assert out.outcome == "echo_ignored"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_echo_role_alongside_inbound_role_applies_only_inbound(
        self, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        pairing = _seed(dal, community_id)
        create_binding(
            dal,
            community_id,
            pairing.id,
            sync_scope="subscriber_tier",
            discord_role_id=_TIER1_ROLE,
            subscriber_tier=1,
        )
        out = await _apply(
            dal, _member(tenant_id, community_id, [_TIER1_ROLE, _MEMBER_ROLE_DISCORD])
        )
        assert out.outcome == "applied"

    async def test_cross_community_and_cross_tenant_refused(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        wrong_guild = await _apply(
            dal, _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD], guild=_OTHER_GUILD)
        )
        wrong_tenant = await _apply(
            dal, _member(tenant_id + 100, community_id, [_MEMBER_ROLE_DISCORD])
        )
        unknown_community = await _apply(
            dal, _member(tenant_id, community_id + 500, [_MEMBER_ROLE_DISCORD])
        )
        assert wrong_guild.outcome == "cross_community_refused"
        assert wrong_tenant.outcome == "cross_tenant_refused"
        assert unknown_community.outcome == "no_tenant"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_other_refusals(self, guild_pairing_db: Any, _flag_on: AsyncMock) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        ev = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD])
        assert (
            await _apply(dal, _member(tenant_id, community_id, ["1"]))
        ).outcome == "no_mapped_roles"
        unlinked = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD], user="1234")
        assert (await _apply(dal, unlinked)).outcome == "unlinked"
        _flag_on.return_value = False
        assert (await _apply(dal, ev)).outcome == "flag_off"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_owner_protected_and_not_a_member(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id, role="community-owner")
        ev = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD])
        assert (await _apply(dal, ev)).outcome == "owner_protected"
        dal(dal.community_members.community_id == community_id).delete()
        dal.commit()
        assert (await _apply(dal, ev)).outcome == "not_a_member"

    async def test_defensive_branches_fail_closed(
        self, guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        ev = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD])
        no_user = inbound.InboundRoleEvent(
            tenant_id,
            community_id,
            "guild.member.update",
            _DISCORD_GUILD_ID,
            None,
            None,
            frozenset({_MEMBER_ROLE_DISCORD}),
        )
        assert (await _apply(dal, no_user)).outcome == "malformed"
        monkeypatch.setattr(inbound, "_resolve_desired_community_role", lambda *_a: None)
        assert (await _apply(dal, ev)).outcome == "no_mapped_roles"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_twitch_to_discord_pairing_is_not_inbound(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        created = create_pairing(
            dal,
            community_id,
            discord_guild_id=_DISCORD_GUILD_ID,
            direction="twitch_to_discord",
            role_name_prefix="[BC]",
            actor_user_id=None,
        )
        update_pairing(dal, community_id, created.id, sync_enabled=True)
        out = await _apply(dal, _member(tenant_id, community_id, ["1"]))
        assert out.outcome == "direction_not_inbound"

    async def test_role_events_are_noted_and_deleted_bound_role_logged(
        self, guild_pairing_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)

        def role_ev(kind: str, role: str) -> inbound.InboundRoleEvent:
            return inbound.InboundRoleEvent(
                tenant_id, community_id, kind, _DISCORD_GUILD_ID, None, role, frozenset()
            )

        assert (await _apply(dal, role_ev("guild.role.create", "5"))).outcome == "role_event_noted"
        assert (await _apply(dal, role_ev("guild.role.delete", "5"))).outcome == "role_event_noted"
        with caplog.at_level(logging.ERROR):
            out = await _apply(dal, role_ev("guild.role.delete", _MEMBER_ROLE_DISCORD))
        assert out.outcome == "bound_role_deleted"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"
        assert _DISCORD_PLATFORM_USER not in caplog.text


_RING = inbound.KeyRing({"k1": bytes([1]) * 32, "k2": bytes([2]) * 32})
_WS = "8f14e45f-ceea-467e-adde-3fb5c9752730"
_EV = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_TP = f"00-{_TRACE}-00f067aa0ba902b7-01"
_TENANT = "acme-corp"  # conftest TENANT_SLUG
_COMMUNITY = "test-community"  # conftest community name
_KEY = f"waddles:t:{_TENANT}:c:{_COMMUNITY}:src:discord:g1"


def _signed(
    event_type: str,
    payload: Any,
    *,
    tid: int,
    cid: int,
    tenant: str = _TENANT,
    community: str = _COMMUNITY,
    version: int = 3,
    kid: str = "k1",
    json_ids: tuple[int, int] | None = None,
    event_id: str = _EV,
) -> dict[bytes, bytes]:
    """A correctly MAC'd v3 envelope entry (MAC over `tid`/`cid`; JSON ids may be swapped)."""
    mac_ids = (tid, cid)
    mac = inbound.compute_binding_mac_v3(
        _RING, kid, mac_ids[0], mac_ids[1], tenant, community, _WS, event_id, _TRACE
    )
    body: dict[str, Any] = {
        "schema_version": version,
        "tenant": tenant,
        "community": community,
        "workstream_id": _WS,
        "event_id": event_id,
        "trace": {"traceparent": _TP},
        "binding": {"kid": kid, "mac": mac},
        "event": {"platform": "discord", "event_type": event_type, "payload": payload},
    }
    body["tenant_id"], body["community_id"] = json_ids or (tid, cid)
    return {b"env": json.dumps(body).encode()}


def _parse(fields: dict[Any, Any], dal: Any, key: str = _KEY) -> Any:
    return inbound.parse_inbound_entry(fields, stream_key=key, ring=_RING, dal=dal)


_MEMBER_PAYLOAD = {"guild_id": "1", "user_id": "2", "role_ids": ["3"]}


class TestMacVectors:
    """Pinned to the Rust `penguin_spine` test vectors (binding.rs) -- one shared oracle."""

    def test_v3_matches_the_rust_pinned_vector(self) -> None:
        v3 = inbound.compute_binding_mac_v3(_RING, "k1", 10, 7, "acme", "main", _WS, _EV, _TRACE)
        assert v3 == "4a7246ec724fb1cdcd88bc37b7243ecd3a10a4eec39d01a8cd3cd24f06db5fc2"

    def test_keyring_parse(self) -> None:
        ring = inbound.KeyRing.parse(" a:0102 , b:ff ,")
        assert ring.key_for("a") == b"\x01\x02" and ring.key_for("z") is None
        for bad in ("", "a", "a:", ":ab", "a:zz"):
            with pytest.raises(ValueError):
                inbound.KeyRing.parse(bad)


class TestParse:
    def test_valid_member_and_role_entries_use_db_ids(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        m = _parse(_signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid), dal)
        assert (m.tenant_id, m.community_id, m.user_id) == (tid, cid, "2")
        assert m.role_ids == {"3"}
        r = _parse(
            _signed("guild.role.delete", {"guild_id": "1", "role_id": "3"}, tid=tid, cid=cid),
            dal,
        )
        assert r.role_id == "3" and r.user_id is None and r.community_id == cid

    def test_other_event_types_are_ignored(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        assert _parse(_signed("message", {"text": "x"}, tid=tid, cid=cid), dal) is None

    @pytest.mark.parametrize(
        "fields",
        [
            {},
            {b"env": b"{nope"},
            {b"env": b"[]"},
            {
                b"env": json.dumps(
                    {
                        "event": {
                            "event_type": "guild.role.create",
                            "platform": "twitch",
                            "payload": {},
                        }
                    }
                ).encode()
            },
            {
                b"env": json.dumps(
                    {
                        "event": {
                            "event_type": "guild.role.create",
                            "platform": "discord",
                            "payload": "x",
                        }
                    }
                ).encode()
            },
        ],
    )
    def test_malformed_raises(self, fields: dict[Any, Any], guild_pairing_db: Any) -> None:
        with pytest.raises(inbound.InboundEventError):
            _parse(fields, guild_pairing_db[0])

    @pytest.mark.parametrize(
        "payload",
        [
            {"guild_id": "abc", "role_id": "3"},
            {"guild_id": "1", "user_id": "2", "role_ids": "x"},
            {"guild_id": "1", "user_id": "2", "role_ids": [3]},
        ],
    )
    def test_malformed_payload_after_verification_raises(
        self, payload: Any, guild_pairing_db: Any
    ) -> None:
        dal, cid, tid, _ = guild_pairing_db
        et = "guild.member.update" if "user_id" in payload else "guild.role.create"
        with pytest.raises(inbound.InboundEventError):
            _parse(_signed(et, payload, tid=tid, cid=cid), dal)


class TestTenantBoundary:
    """Forged / misrouted / id-substituted entries are refused (HIGH finding)."""

    def _expect(self, fields: Any, dal: Any, reason: str, key: str = _KEY) -> None:
        with pytest.raises(inbound.TenantBoundaryError) as exc:
            _parse(fields, dal, key)
        assert exc.value.reason == reason

    def test_forged_mac(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid)
        env = json.loads(f[b"env"])
        env["binding"]["mac"] = "0" * 64
        self._expect({b"env": json.dumps(env).encode()}, dal, "mac_mismatch")

    def test_forged_via_swapped_numeric_ids_in_json(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        f = _signed(
            "guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid, json_ids=(tid + 1, cid)
        )
        self._expect(f, dal, "mac_mismatch")  # ids are inside the v3 MAC

    def test_json_ids_differing_from_stream_scope(self, guild_pairing_db: Any) -> None:
        """A validly-MACed envelope whose signed ids are NOT the stream scope's DB ids."""
        dal, cid, tid, _ = guild_pairing_db
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid + 50, cid=cid + 50)
        self._expect(f, dal, "json_ids_differ_from_scope")

    def test_mismatched_scope_vs_stream_key(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid)
        self._expect(f, dal, "tenant_mismatch", "waddles:t:other:c:test-community:src:discord:g")
        self._expect(f, dal, "community_mismatch", f"waddles:t:{_TENANT}:c:main:src:discord:g")
        self._expect(f, dal, "bad_stream_key", "garbage")
        self._expect(f, dal, "bad_stream_key", "waddles:t::c:main:src:discord:g")
        self._expect(f, dal, "bad_stream_key", f"waddles:t:{_TENANT}:c::src:discord:g")
        self._expect(f, dal, "bad_stream_key", f"waddles:t:{_TENANT}:c:_tenant:src:discord:g")

    def test_unknown_scope_in_db(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid, tenant="nope")
        self._expect(f, dal, "unknown_tenant", "waddles:t:nope:c:test-community:src:discord:g")
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid, community="zzz")
        self._expect(f, dal, "unknown_community", f"waddles:t:{_TENANT}:c:zzz:src:discord:g")

    def test_binding_structure_and_versions(self, guild_pairing_db: Any) -> None:
        dal, cid, tid, _ = guild_pairing_db
        base = json.loads(_signed("guild.member.update", _MEMBER_PAYLOAD, tid=tid, cid=cid)[b"env"])

        def run(mut: Any, reason: str) -> None:
            env = json.loads(json.dumps(base))
            mut(env)
            self._expect({b"env": json.dumps(env).encode()}, dal, reason)

        run(lambda e: e.pop("binding"), "no_binding")
        run(lambda e: e["binding"].update(kid="zz"), "unknown_kid")
        run(lambda e: e.update(workstream_id=5), "malformed_binding_fields")
        run(lambda e: e.update(schema_version=9), "unsupported_schema_version")
        run(lambda e: e.pop("tenant_id"), "v3_missing_ids")
        run(lambda e: e.update(schema_version=2), "unsupported_schema_version")
        run(lambda e: e.pop("trace"), "mac_mismatch")  # trace id is MAC input

    def test_v2_entry_is_refused_even_with_a_valid_v2_mac(self, guild_pairing_db: Any) -> None:
        import hashlib
        import hmac

        dal, cid, tid, _ = guild_pairing_db
        msg = (_TENANT + _COMMUNITY + _WS + _EV + _TRACE).encode()
        env = {
            "schema_version": 2,
            "tenant": _TENANT,
            "community": _COMMUNITY,
            "workstream_id": _WS,
            "event_id": _EV,
            "trace": {"traceparent": _TP},
            "binding": {
                "kid": "k1",
                "mac": hmac.new(bytes([1]) * 32, msg, hashlib.sha256).hexdigest(),
            },
            "event": {
                "platform": "discord",
                "event_type": "guild.role.update",
                "payload": {"guild_id": "1", "role_id": "3"},
            },
        }
        self._expect({b"env": json.dumps(env).encode()}, dal, "unsupported_schema_version")

    @pytest.mark.parametrize("ids", [(2**63, 7), (-(2**63) - 1, 7), (5, 2**31), (5, -(2**31) - 1)])
    def test_out_of_range_ids_are_refused_not_crashed(
        self, ids: tuple[int, int], guild_pairing_db: Any
    ) -> None:
        dal = guild_pairing_db[0]
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=1, cid=1, json_ids=ids)
        self._expect(f, dal, "ids_out_of_range")

    @pytest.mark.parametrize("ids", [(True, 7), ("5", 7), (5.5, 7), (None, 7), (5, None)])
    def test_non_int_ids_are_refused(self, ids: tuple[Any, Any], guild_pairing_db: Any) -> None:
        dal = guild_pairing_db[0]
        f = _signed("guild.member.update", _MEMBER_PAYLOAD, tid=1, cid=1)
        env = json.loads(f[b"env"])
        env["tenant_id"], env["community_id"] = ids
        reason = "v3_missing_ids" if None in ids else "ids_out_of_range"
        self._expect({b"env": json.dumps(env).encode()}, dal, reason)

    def test_any_exception_in_verification_becomes_malformed(
        self, guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = guild_pairing_db[0]
        monkeypatch.setattr(inbound, "_verify_binding_v3", lambda *_a: 1 / 0)
        self._expect(
            _signed("guild.member.update", _MEMBER_PAYLOAD, tid=1, cid=1),
            dal,
            "malformed_binding_fields",
        )


class _FakeRedis:
    def __init__(self, streams: dict[str, list[tuple[str, dict[Any, Any]]]]) -> None:
        self.streams = streams
        self.acked: list[str] = []
        self.groups: set[str] = set()
        self.busy = False
        self.kv: dict[str, Any] = {}
        self.dlq: list[dict[str, Any]] = []
        self.fail_xack = False

    async def scan_iter(self, match: str) -> Any:
        assert match == inbound.DISCORD_STREAM_PATTERN
        for k in self.streams:
            yield k.encode()

    async def xgroup_create(self, stream: str, group: str, id: str, mkstream: bool) -> None:
        if stream in self.groups or self.busy:
            raise redis.exceptions.ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups.add(stream)

    async def xreadgroup(
        self, group: str, consumer: str, streams: dict[str, str], count: int
    ) -> Any:
        (stream,) = streams
        entries, self.streams[stream] = self.streams[stream], []
        return [(stream, entries)] if entries else []

    async def xack(self, stream: str, group: str, entry_id: str) -> None:
        if self.fail_xack:
            raise ConnectionError("valkey down")
        self.acked.append(entry_id)

    async def set(self, key: str, value: str, nx: bool, ex: int) -> bool:
        assert ex > 0 and nx
        if key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def xadd(
        self, stream: str, fields: dict[str, Any], maxlen: int, approximate: bool
    ) -> None:
        if self.fail_xack:
            raise ConnectionError("valkey down")
        assert stream == inbound._DLQ_STREAM
        self.dlq.append(fields)


_GOOD = {
    "guild_id": _DISCORD_GUILD_ID,
    "user_id": _DISCORD_PLATFORM_USER,
    "role_ids": [_MEMBER_ROLE_DISCORD],
}


def _batch(fake: Any, dal: Any, confirm: Any = None) -> Any:
    return inbound.run_inbound_batch(dal, fake, _RING, confirm or _Confirm({_MEMBER_ROLE_DISCORD}))


class TestPayloadConfirmation:
    """The payload is not MAC-covered: Discord's own state decides."""

    async def test_forged_payload_with_a_valid_mac_is_refused(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        ev = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD])  # attacker claims a role
        confirm = _Confirm({"999"})  # Discord says the member does not hold it
        out = await _apply(dal, ev, confirm)
        assert out.outcome == "payload_mismatch"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_absent_member_and_unavailable_discord_refuse(
        self, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        ev = _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD])
        assert (await _apply(dal, ev, _Confirm(None))).outcome == "payload_mismatch"
        for exc in (inbound.TransportUnavailable("x"), inbound.DiscordSyncError("x")):
            out = await _apply(dal, ev, _Confirm(None, raises=exc))
            assert out.outcome == "confirm_unavailable"
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_confirmation_uses_the_paired_guild_and_resolved_tenant(
        self, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        confirm = _Confirm({_MEMBER_ROLE_DISCORD})
        await _apply(dal, _member(tenant_id, community_id, [_MEMBER_ROLE_DISCORD]), confirm)
        (call,) = confirm.calls
        assert call["guild_id"] == _DISCORD_GUILD_ID and call["tenant_id"] == tenant_id
        assert call["user_id"] == _DISCORD_PLATFORM_USER

    async def test_api_confirm_resolves_the_tenant_token_and_reads_the_member(
        self, guild_pairing_db: Any
    ) -> None:
        from services.credential_resolver import PlatformCredentials

        dal = guild_pairing_db[0]
        seen: dict[str, Any] = {}

        class Resolver:
            async def resolve(self, dal: Any, **kw: Any) -> Any:
                seen.update(kw)
                return PlatformCredentials(
                    tenant_id=3, platform="discord", payload={"bot_token": "tok"}, source="tenant"
                )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/guilds/5/members/7")
            assert request.headers["authorization"] == "Bot tok"
            return httpx.Response(200, json={"roles": ["10", "11"]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c = inbound.ApiDiscordConfirm(http, Resolver())
            got = await c.member_roles(dal, tenant_id=3, is_global=False, guild_id="5", user_id="7")
        assert got == {"10", "11"} and seen["tenant_id"] == 3 and seen["platform"] == "discord"

    async def test_api_confirm_without_a_token_is_unavailable(self, guild_pairing_db: Any) -> None:
        from services.credential_resolver import PlatformCredentials

        class Resolver:
            async def resolve(self, dal: Any, **kw: Any) -> Any:
                return PlatformCredentials(
                    tenant_id=3, platform="discord", payload={}, source="tenant"
                )

        async with httpx.AsyncClient() as http:
            c = inbound.ApiDiscordConfirm(http, Resolver())
            with pytest.raises(inbound.TransportUnavailable):
                await c.member_roles(
                    guild_pairing_db[0], tenant_id=3, is_global=False, guild_id="5", user_id="7"
                )


class TestBatch:
    async def test_drain_applies_acks_and_counts(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)

        def mk(et: str, pl: Any, n: int) -> Any:
            return _signed(et, pl, tid=tenant_id, cid=community_id, event_id=f"e{n}")

        forged = json.loads(mk("guild.member.update", _GOOD, 9)[b"env"])
        forged["binding"]["mac"] = "f" * 64
        fake = _FakeRedis(
            {
                _KEY: [
                    ("1-0", mk("guild.member.update", _GOOD, 1)),
                    ("2-0", mk("message", {"text": "hi"}, 2)),
                    ("3-0", {b"env": b"junk"}),
                    ("4-0", {b"env": json.dumps(forged).encode()}),
                ]
            }
        )
        res = await _batch(fake, dal)
        assert (
            res.streams,
            res.examined,
            res.role_events,
            res.applied,
            res.poison,
            res.violations,
            res.failed,
            res.replayed,
        ) == (1, 4, 1, 1, 1, 1, 0, 0)
        assert sorted(fake.acked) == ["1-0", "2-0", "3-0", "4-0"]
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "vip"
        fake.busy = True  # existing group tolerated
        assert (await _batch(fake, dal)).examined == 0

    async def test_replayed_event_id_is_applied_once(self, guild_pairing_db: Any) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        e = _signed("guild.member.update", _GOOD, tid=tenant_id, cid=community_id)
        fake = _FakeRedis({_KEY: [("1-0", e), ("2-0", e)]})
        confirm = _Confirm({_MEMBER_ROLE_DISCORD})
        res = await _batch(fake, dal, confirm)
        assert (res.applied, res.replayed, res.failed) == (1, 1, 0)
        assert len(confirm.calls) == 1, "the replay never reached Discord or the DB"
        assert sorted(fake.acked) == ["1-0", "2-0"]

    async def test_forged_payload_in_a_batch_is_refused_after_confirmation(
        self, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        e = _signed("guild.member.update", _GOOD, tid=tenant_id, cid=community_id)
        res = await _batch(_FakeRedis({_KEY: [("1-0", e)]}), dal, _Confirm({"1"}))
        assert (res.role_events, res.applied) == (1, 0)
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "member"

    async def test_batch_continues_past_poison_and_unexpected_entries(
        self, guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        _seed(dal, community_id)
        real = inbound.parse_inbound_entry

        def flaky(fields: Any, **kw: Any) -> Any:
            if fields.get(b"boom"):
                raise RuntimeError("parser bug")
            return real(fields, **kw)

        monkeypatch.setattr(inbound, "parse_inbound_entry", flaky)
        good = _signed("guild.member.update", _GOOD, tid=tenant_id, cid=community_id, event_id="g")
        huge = _signed(
            "guild.member.update", _GOOD, tid=1, cid=1, json_ids=(2**80, 1), event_id="h"
        )
        fake = _FakeRedis(
            {_KEY: [("1-0", {b"boom": b"1"}), ("2-0", huge[b"env"] and huge), ("3-0", good)]}
        )
        res = await _batch(fake, dal)
        assert (res.failed, res.violations, res.applied) == (1, 1, 1)
        assert sorted(fake.acked) == ["1-0", "2-0", "3-0"], "nothing left pending"
        assert fake.dlq == [{"stream": _KEY, "entry_id": "1-0", "error": "RuntimeError"}]

    async def test_apply_failure_is_dead_lettered_and_acked(
        self, guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        monkeypatch.setattr(
            inbound, "apply_inbound_event", AsyncMock(side_effect=RuntimeError("db"))
        )
        e = _signed(
            "guild.role.update", {"guild_id": "1", "role_id": "2"}, tid=tenant_id, cid=community_id
        )
        fake = _FakeRedis({_KEY: [("1-0", e)]})
        res = await _batch(fake, dal)
        assert (res.failed, fake.acked, len(fake.dlq)) == (1, ["1-0"], 1)

    async def test_dlq_and_ack_failures_never_abort_the_batch(
        self, guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, tenant_id, _ = guild_pairing_db
        monkeypatch.setattr(
            inbound, "apply_inbound_event", AsyncMock(side_effect=RuntimeError("db"))
        )
        mk = lambda n: _signed(  # noqa: E731
            "guild.role.update",
            {"guild_id": "1", "role_id": "2"},
            tid=tenant_id,
            cid=community_id,
            event_id=f"x{n}",
        )
        fake = _FakeRedis({_KEY: [("1-0", mk(1)), ("2-0", mk(2))]})
        fake.fail_xack = True
        res = await _batch(fake, dal)
        assert (res.examined, res.failed) == (2, 2)

    async def test_group_create_error_other_than_busy_propagates(self) -> None:
        class Bad(_FakeRedis):
            async def xgroup_create(self, *a: Any, **k: Any) -> None:
                raise redis.exceptions.ResponseError("WRONGTYPE")

        with pytest.raises(redis.exceptions.ResponseError):
            await inbound.run_inbound_batch(object(), Bad({"s": []}), _RING, _Confirm(set()))


class TestMain:
    async def test_main_prints_denominators(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from contextlib import contextmanager

        @contextmanager
        def fake_open(_url: str) -> Any:
            yield type("D", (), {"dal": object()})()

        batches = iter(
            [inbound.InboundBatchResult(1, 2, 1, 1, 0, 0, 0), inbound.InboundBatchResult()]
        )
        monkeypatch.setenv("DATABASE_URL", "sqlite://x")
        monkeypatch.setenv("ENVELOPE_BINDING_KEYS", "k1:0102")
        monkeypatch.setattr(inbound, "open_worker_dal", fake_open)
        monkeypatch.setattr(inbound, "build_client", lambda: object())
        monkeypatch.setattr(
            inbound, "run_inbound_batch", AsyncMock(side_effect=lambda *a, **k: next(batches))
        )
        assert await inbound.main() == 0
        out = capsys.readouterr()
        assert "examined=2 role_events=1 applied=1" in out.out
        assert "zero Discord streams" in out.err or "streams=1" in out.out

    async def test_main_exit_code_reflects_failures_and_idle_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from contextlib import contextmanager

        @contextmanager
        def fake_open(_url: str) -> Any:
            yield type("D", (), {"dal": object()})()

        monkeypatch.setenv("DATABASE_URL", "sqlite://x")
        monkeypatch.setenv("ENVELOPE_BINDING_KEYS", "k1:0102")
        monkeypatch.setattr(inbound, "open_worker_dal", fake_open)
        monkeypatch.setattr(inbound, "build_client", lambda: object())
        monkeypatch.setattr(
            inbound, "run_inbound_batch", AsyncMock(return_value=inbound.InboundBatchResult())
        )
        assert await inbound.main() == 0
        assert "zero Discord streams" in capsys.readouterr().err
        monkeypatch.setattr(
            inbound,
            "run_inbound_batch",
            AsyncMock(
                side_effect=[
                    inbound.InboundBatchResult(1, 1, 1, 0, 0, 1, 0),
                    inbound.InboundBatchResult(),
                ]
            ),
        )
        assert await inbound.main() == 1
