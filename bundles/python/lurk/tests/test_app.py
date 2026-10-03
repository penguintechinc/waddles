"""Host-native tests for the `lurk` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, extended with a fake `kv` import (this bundle's own
`storage.kv` permission) alongside `flags`/`relay`/`log`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import LURK_REPLY, UNLURK_REPLY, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-03T00:00:00.000Z",
    )


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: `flags.enabled` True, `kv.set/delete` and `relay.push` recorded."""
    kv_calls: list[tuple[str, ...]] = []
    relay_calls: list[tuple[str, str]] = []

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    kv_mod = types.SimpleNamespace(
        set=lambda key, value, ttl: kv_calls.append(("set", key, value, ttl)),
        delete=lambda key: kv_calls.append(("delete", key)),
        get=lambda key: None,
        increment=lambda key, delta, ttl: 1,
    )
    relay_mod = types.SimpleNamespace(push=lambda provider, msg: relay_calls.append((provider, msg)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}, write=lambda *a: None
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(kv_calls=kv_calls, relay_calls=relay_calls)


@pytest.mark.parametrize("text", ["!lurk", "!LURK", "  !lurk  "])
def test_lurk_matches_case_insensitively_and_with_whitespace(text: str, fake_host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "lurk"


def test_unlurk_matches(fake_host) -> None:
    result = _run(transform(_sample_event("!unlurk")))
    assert result is not None
    assert result.payload["command"] == "unlurk"


@pytest.mark.parametrize("text", ["!lurking", "lurk", "!unlurked", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!lurk"))) is None


def _sample_envelope(
    platform: str, command: str, *, community: str | None = "comm-1", actor: str | None = "viewer-1"
) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.lurk",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload={"command": command, "channel_id": "12345"},
            occurred_at="2026-10-03T00:00:00.000Z",
        ),
        ts="2026-10-03T00:00:00.000Z",
    )


def test_dispatch_lurk_sets_kv_keyed_by_community_and_actor_not_username(fake_host) -> None:
    envelope = _sample_envelope("twitch", "lurk")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "lurk"
    op, key, value, ttl = fake_host.kv_calls[0]
    assert op == "set"
    assert key == "lurk:comm-1:viewer-1"  # opaque actor id, never a raw display name
    assert value == b"1"
    assert ttl == 24 * 60 * 60
    provider, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json) == {"channel": "12345", "text": LURK_REPLY}


def test_dispatch_unlurk_deletes_the_same_key_shape(fake_host) -> None:
    envelope = _sample_envelope("discord", "unlurk")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "unlurk"
    assert fake_host.kv_calls[0] == ("delete", "lurk:comm-1:viewer-1")
    provider, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json) == {"channel": "12345", "text": UNLURK_REPLY}


def test_dispatch_falls_back_to_tenant_sentinel_when_community_is_none(fake_host) -> None:
    envelope = _sample_envelope("twitch", "lurk", community=None)
    _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls[0][1] == "lurk:tenant:viewer-1"


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.lurk",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "lurk", "channel_id": None},
            occurred_at="2026-10-03T00:00:00.000Z",
        ),
        ts="2026-10-03T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_on_an_unrecognized_command(fake_host) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized lurk command"):
        _run(dispatch(envelope, {}, http_client=None))
