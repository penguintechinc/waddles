"""Connector-path event sync: signed-request client + engine routing and kill-switch.

Valkey is a fake list store standing in for svc-action's consumer; the MAC golden vector is
shared with `core/svc_action/src/capabilities/event_sync_tests.rs` (cross-language compat).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services import event_discord_sync_service as svc
from services import event_sync_connector_client as ec

_KEYS = "r1:a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
_GUILD = "555000111"  # gitleaks:allow - fake snowflake
_PAYLOAD = svc.DiscordEventPayload(
    name="Movie night",
    description="snacks",
    scheduled_start_time="2026-12-01T19:00:00+00:00",
    scheduled_end_time="2026-12-01T20:00:00+00:00",
    location="Online",
)


class FakeValkey:
    """lpush/blpop store; `reply` simulates svc-action answering the request."""

    def __init__(self, reply: Any = None, *, fail: bool = False) -> None:
        """Hold the canned reply / outage flag and the recorded requests."""
        self.reply = reply
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def lpush(self, key: str, value: str) -> None:
        if self.fail:
            raise ConnectionError("down")
        assert key == ec.REQUEST_QUEUE
        self.requests.append(json.loads(value))

    async def blpop(self, key: str, timeout: int) -> Any:  # noqa: ASYNC109
        assert key == f"{ec.RESULT_PREFIX}{self.requests[-1]['request_id']}"
        assert timeout >= 1
        return None if self.reply is None else (key, json.dumps(self.reply))

    async def aclose(self) -> None:
        self.closed = True


def _client(valkey: FakeValkey) -> ec.ConnectorEventClient:
    return ec.ConnectorEventClient(
        valkey,
        tenant_id=3,
        community_id=7,
        keys=ec.parse_key_ring(_KEYS, "r1"),
        error_cls=svc.DiscordEventSyncError,
        not_found_cls=svc.DiscordEventNotFoundError,
        clock=lambda: 1_700_000_000.0,
    )


def test_mac_golden_vector_matches_rust():
    keys, kid = ec.parse_key_ring(_KEYS, "r1")
    sig = ec.sign_request(
        keys[kid],
        kid,
        action="update",
        expect_guild_id=_GUILD,
        discord_event_id="4242",
        event_json='{"name":"n"}',
        request_id="req-1",
        exp_ms=1_700_000_000_000,
        community_id=7,
        tenant_id=3,
    )
    assert sig == "r1:14780dbf17556ab585cf5a61f7ae172b67055b06c8d0923e912b4f8eeb52e7ea"


@pytest.mark.parametrize(
    ("raw", "active"),
    [
        ("", "r1"),
        ("r1:zz", "r1"),
        ("r1:0011", "r1"),
        (":a0a1a2a3a4a5a6a7a8a9aaabacadaeaf", "r1"),
        ("noseparator", "r1"),
        (_KEYS, None),
        (_KEYS, "other"),
    ],
)
def test_bad_key_rings_fail_loud(raw, active):
    with pytest.raises(ec.RequestKeyError):
        ec.parse_key_ring(raw, active)


def test_keys_from_env(monkeypatch):
    monkeypatch.delenv("EVENT_SYNC_REQUEST_KEYS", raising=False)
    with pytest.raises(ec.RequestKeyError):
        ec.keys_from_env()
    monkeypatch.setenv("EVENT_SYNC_REQUEST_KEYS", _KEYS)
    monkeypatch.setenv("EVENT_SYNC_REQUEST_ACTIVE_KID", "r1")
    assert ec.keys_from_env()[1] == "r1"


async def test_create_enqueues_a_signed_request_and_returns_the_event_id():
    v = FakeValkey({"ok": True, "id": "987654321"})
    assert await _client(v).create_scheduled_event(guild_id=_GUILD, payload=_PAYLOAD) == "987654321"
    req = v.requests[0]
    assert (req["action"], req["tenant_id"], req["community_id"]) == ("create", 3, 7)
    assert req["expect_guild_id"] == _GUILD and req["discord_event_id"] is None
    assert json.loads(req["event_json"])["name"] == "Movie night"
    # flat field object, never the Discord REST body
    assert "entity_type" not in json.loads(req["event_json"])
    keys, kid = ec.parse_key_ring(_KEYS, "r1")
    assert req["sig"] == ec.sign_request(
        keys[kid],
        kid,
        action="create",
        expect_guild_id=_GUILD,
        discord_event_id="",
        event_json=req["event_json"],
        request_id=req["request_id"],
        exp_ms=req["exp_ms"],
        community_id=7,
        tenant_id=3,
    )


async def test_update_and_cancel_address_the_event():
    v = FakeValkey({"ok": True})
    c = _client(v)
    await c.patch_scheduled_event(guild_id=_GUILD, discord_event_id="42", payload=_PAYLOAD)
    await c.cancel_scheduled_event(guild_id=_GUILD, discord_event_id="42")
    assert [(r["action"], r["discord_event_id"]) for r in v.requests] == [
        ("update", "42"),
        ("cancel", "42"),
    ]
    assert v.requests[0]["event_json"] is not None and v.requests[1]["event_json"] is None


async def test_failures_are_loud_and_not_found_maps_to_drift():
    with pytest.raises(svc.DiscordEventNotFoundError):
        await _client(FakeValkey({"ok": False, "code": ec.NOT_FOUND_CODE})).cancel_scheduled_event(
            guild_id=_GUILD, discord_event_id="1"
        )
    for reply, match in [
        ({"ok": False, "code": "guild_mismatch"}, "guild_mismatch"),
        (None, "not confirmed"),
        ({"nope": 1}, "unreadable"),
        ({"ok": True}, "no event id"),
        ({"ok": True, "id": "abc"}, "no event id"),
    ]:
        with pytest.raises(svc.DiscordEventSyncError, match=match):
            await _client(FakeValkey(reply)).create_scheduled_event(
                guild_id=_GUILD, payload=_PAYLOAD
            )
    with pytest.raises(svc.DiscordEventSyncError, match="valkey unavailable"):
        await _client(FakeValkey(fail=True)).create_scheduled_event(
            guild_id=_GUILD, payload=_PAYLOAD
        )


def test_factory_missing_keys_fails_loud_never_falls_back():
    def no_keys():
        raise ec.RequestKeyError("missing")

    f = svc._ConnectorClientFactory(valkey_factory=FakeValkey, keys_loader=no_keys)
    with pytest.raises(svc.DiscordEventSyncError, match="signing unavailable"):
        f.open(3, 7)
    f = svc._ConnectorClientFactory(
        valkey_factory=FakeValkey, keys_loader=lambda: ec.parse_key_ring(_KEYS, "r1")
    )
    client, valkey = f.open(3, 7)
    assert isinstance(client, ec.ConnectorEventClient) and isinstance(valkey, FakeValkey)


async def test_kill_switch_flag_semantics(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def fake_flag(name, tenant=None):
        seen.append((name, tenant))
        return True

    monkeypatch.setattr(svc, "feature_enabled", fake_flag)
    assert await svc._legacy_httpx_enabled("acme") is True
    assert seen == [("waddles.disable-connector-event-sync", "acme")]

    async def off(name, tenant=None):
        return False

    monkeypatch.setattr(svc, "feature_enabled", off)
    assert await svc._legacy_httpx_enabled("acme") is False
    monkeypatch.setattr(svc, "feature_enabled", None)
    assert await svc._legacy_httpx_enabled("acme") is False


# ---- engine routing (real sqlite fixtures) --------------------------------------------------


def _flags(monkeypatch, *, kill_switch: bool):
    async def flag(name, tenant=None):
        if name == svc.DISABLE_CONNECTOR_EVENT_SYNC_FLAG:
            return kill_switch
        return True

    monkeypatch.setattr(svc, "feature_enabled", flag)


class _NeverResolver:
    async def resolve(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("connector path must not resolve a Discord credential")


class _LegacyClient:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create_scheduled_event(self, *, guild_id: str, payload: Any) -> str:
        self.created.append(guild_id)
        return "111"

    async def patch_scheduled_event(self, **k: Any) -> None:  # pragma: no cover
        raise AssertionError

    async def cancel_scheduled_event(self, **k: Any) -> None:  # pragma: no cover
        raise AssertionError


def _event_and_pairing(event_sync_db: Any):
    from services.guild_pairing import create_pairing, update_pairing

    dal, community_id, _tenant_id, _g, event_id = event_sync_db
    created = create_pairing(
        dal,
        community_id,
        discord_guild_id=_GUILD,
        direction="bidirectional",
        role_name_prefix="[ES]",
        actor_user_id=None,
    )
    update_pairing(dal, community_id, created.id, event_sync_enabled=True)
    return dal, dal(dal.calendar_events.id == event_id).select().first(), event_id


def _target(valkey: FakeValkey, legacy: _LegacyClient | None = None) -> svc.PlatformEventTarget:
    return svc.DiscordScheduledEventTarget(
        credential_resolver=_NeverResolver() if legacy is None else _OkResolver(),
        make_client=lambda token: legacy,
        connector=svc._ConnectorClientFactory(
            valkey_factory=lambda: valkey, keys_loader=lambda: ec.parse_key_ring(_KEYS, "r1")
        ),
    )


class _OkResolver:
    async def resolve(self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str):
        from services.credential_resolver import PlatformCredentials

        return PlatformCredentials(
            tenant_id=tenant_id, platform=platform, payload={"bot_token": "t"}, source="tenant"
        )


async def test_default_routes_through_the_connector_without_a_discord_credential(
    event_sync_db, monkeypatch
):
    _flags(monkeypatch, kill_switch=False)
    dal, event_row, event_id = _event_and_pairing(event_sync_db)
    valkey = FakeValkey({"ok": True, "id": "900000000000000001"})
    result = await svc.sync_event(dal, event_row, action="create", targets=[_target(valkey)])
    assert (result.sync_status, result.discord_event_id) == ("synced", "900000000000000001")
    req = valkey.requests[0]
    assert (req["action"], req["expect_guild_id"]) == ("create", _GUILD)
    assert valkey.closed
    row = dal(dal.calendar_events.id == event_id).select().first()
    assert row.discord_event_id == "900000000000000001"


async def test_kill_switch_on_uses_the_legacy_direct_path(event_sync_db, monkeypatch):
    _flags(monkeypatch, kill_switch=True)
    dal, event_row, _ = _event_and_pairing(event_sync_db)
    valkey = FakeValkey({"ok": True, "id": "1"})
    legacy = _LegacyClient()
    result = await svc.sync_event(
        dal, event_row, action="create", targets=[_target(valkey, legacy)]
    )
    assert result.sync_status == "synced" and legacy.created == [_GUILD]
    assert valkey.requests == []


async def test_connector_failure_is_a_loud_sync_error_never_a_silent_fallback(
    event_sync_db, monkeypatch
):
    _flags(monkeypatch, kill_switch=False)
    dal, event_row, _ = _event_and_pairing(event_sync_db)
    legacy = _LegacyClient()
    valkey = FakeValkey({"ok": False, "code": "relay_unavailable"})
    result = await svc.sync_event(
        dal, event_row, action="create", targets=[_target(valkey, legacy)]
    )
    assert result.sync_status == "sync_error"
    assert "relay_unavailable" in (result.sync_error or "")
    assert legacy.created == [], "must not fall back to the direct Discord call"
