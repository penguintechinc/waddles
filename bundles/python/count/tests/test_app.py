"""Host-native tests for the `count` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`flags`/`log`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types

import pytest

from app import _kv_key, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _expected_key(community: str | None, actor: str | None) -> str:
    """Mirror `app._kv_key`'s hashing so tests assert the real contract, not a literal."""
    pseudonym = hashlib.sha256(
        f"{community or 'tenant'}:{actor or 'anonymous'}".encode()
    ).hexdigest()
    return f"count:{pseudonym}"


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
    log_calls: list[tuple[int, str, str]] = []
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
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(increments=increments, relay_calls=relay_calls, log_calls=log_calls)


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


def test_dispatch_increments_kv_keyed_by_hashed_community_and_actor(fake_host) -> None:
    envelope = _sample_envelope("twitch")
    result = _run(dispatch(envelope, {}, http_client=None))

    key, delta, ttl = fake_host.increments[0]
    assert key == _expected_key("comm-1", "viewer-1")
    assert "viewer-1" not in key  # raw actor must never appear in the stored kv key
    assert delta == 1
    assert ttl == 0  # no expiry -- a persistent running total
    assert result.detail == "total=1"
    provider, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json) == {"channel": "12345", "text": "You've been counted 1 time!"}


def test_kv_key_is_a_non_reversible_hash_not_the_raw_actor() -> None:
    assert _kv_key("comm-1", "viewer-1") == _expected_key("comm-1", "viewer-1")
    assert "viewer-1" not in _kv_key("comm-1", "viewer-1")


def test_transform_and_dispatch_never_log_the_raw_actor(fake_host) -> None:
    _run(transform(_sample_event("!count")))
    envelope = _sample_envelope("twitch")
    _run(dispatch(envelope, {}, http_client=None))

    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_dispatch_reply_pluralizes_after_the_first_count(fake_host) -> None:
    envelope = _sample_envelope("twitch")
    _run(dispatch(envelope, {}, http_client=None))  # total=1
    _run(dispatch(envelope, {}, http_client=None))  # total=2

    _, message_json = fake_host.relay_calls[1]
    assert json.loads(message_json)["text"] == "You've been counted 2 times!"


def test_dispatch_falls_back_to_tenant_sentinel_when_community_is_none(fake_host) -> None:
    envelope = _sample_envelope("twitch", community=None)
    _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.increments[0][0] == _expected_key(None, "viewer-1")


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
