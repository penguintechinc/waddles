"""Host-native tests for the `count` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`flags`/`log`).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import dispatch, transform
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
    """Fake WIT host: `flags.enabled` True, `kv.increment` returns a scripted sequence."""
    increments: list[tuple[str, int, int]] = []
    relay_calls: list[tuple[str, str]] = []
    totals = iter([1, 2, 3, 4, 5])

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    kv_mod = types.SimpleNamespace(
        increment=lambda key, delta, ttl: (
            increments.append((key, delta, ttl)) or next(totals)
        ),
        get=lambda key: None,
        set=lambda key, value, ttl: None,
        delete=lambda key: None,
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
    return types.SimpleNamespace(increments=increments, relay_calls=relay_calls)


@pytest.mark.parametrize("text", ["!count", "!COUNT", "  !count  "])
def test_count_matches_case_insensitively_and_with_whitespace(text: str, fake_host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["channel_id"] == "12345"


@pytest.mark.parametrize("text", ["!counting", "count", "!count me", "hello", ""])
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

    assert _run(transform(_sample_event("!count"))) is None


def _sample_envelope(
    platform: str, *, community: str | None = "comm-1", actor: str | None = "viewer-1"
) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.count",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload={"channel_id": "12345"},
            occurred_at="2026-10-03T00:00:00.000Z",
        ),
        ts="2026-10-03T00:00:00.000Z",
    )


def test_dispatch_increments_kv_keyed_by_community_and_actor_not_username(fake_host) -> None:
    envelope = _sample_envelope("twitch")
    result = _run(dispatch(envelope, {}, http_client=None))

    key, delta, ttl = fake_host.increments[0]
    assert key == "count:comm-1:viewer-1"  # opaque actor id, never a raw display name
    assert delta == 1
    assert ttl == 0  # no expiry -- a persistent running total
    assert result.detail == "total=1"
    provider, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json) == {"channel": "12345", "text": "You've been counted 1 time!"}


def test_dispatch_reply_pluralizes_after_the_first_count(fake_host) -> None:
    envelope = _sample_envelope("twitch")
    _run(dispatch(envelope, {}, http_client=None))  # total=1
    _run(dispatch(envelope, {}, http_client=None))  # total=2

    _, message_json = fake_host.relay_calls[1]
    assert json.loads(message_json)["text"] == "You've been counted 2 times!"


def test_dispatch_falls_back_to_tenant_sentinel_when_community_is_none(fake_host) -> None:
    envelope = _sample_envelope("twitch", community=None)
    _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.increments[0][0] == "count:tenant:viewer-1"


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.count",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"channel_id": None},
            occurred_at="2026-10-03T00:00:00.000Z",
        ),
        ts="2026-10-03T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.increments == []
