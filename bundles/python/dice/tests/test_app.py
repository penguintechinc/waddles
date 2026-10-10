"""Host-native tests for the `dice` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/eightball/tests/test_app.py`'s own docstring for
the fake-`wit_world` approach this mirrors.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from app import USAGE_TEXT, dispatch, transform
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
    relay_calls: list[tuple[str, str]] = []
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}, write=lambda *a: None
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return relay_calls


def test_dice_with_no_spec_uses_the_1d20_default(fake_host) -> None:
    result = _run(transform(_sample_event("!dice")))
    assert result is not None
    assert "total" in result.payload["text"]


def test_dice_with_a_valid_spec_is_bounds_respected(fake_host) -> None:
    result = _run(transform(_sample_event("!dice 2d6")))
    assert result is not None
    assert "total" in result.payload["text"]


@pytest.mark.parametrize("spec", ["0d6", "101d6", "1d1", "1d1001", "abc", "1dx"])
def test_out_of_bounds_or_malformed_spec_returns_usage_text(
    spec: str, fake_host
) -> None:
    result = _run(transform(_sample_event(f"!dice {spec}")))
    assert result is not None
    assert result.payload["text"] == USAGE_TEXT


@pytest.mark.parametrize("text", ["!diceing", "roll", "!dicecall 1d6", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!dice"))) is None


def _sample_envelope(
    platform: str, text: str, *, channel_id: str | None = "12345"
) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.dice",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-03T00:00:00.000Z",
        ),
        ts="2026-10-03T00:00:00.000Z",
    )


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_the_events_own_origin_platform(
    platform: str, fake_host
) -> None:
    envelope = _sample_envelope(platform, "\U0001f3b2 rolls [7] (total 7)")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.transport == platform
    provider, message_json = fake_host[0]
    assert provider == platform
    assert json.loads(message_json) == {
        "channel": "12345",
        "text": "\U0001f3b2 rolls [7] (total 7)",
    }


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _sample_envelope("twitch", "some roll", channel_id=None)
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host == []


def test_transform_and_dispatch_never_log_the_raw_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_calls: list[tuple[int, str, str]] = []
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    relay_mod = types.SimpleNamespace(push=lambda provider, msg: None)
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    _run(transform(_sample_event("!dice")))
    envelope = _sample_envelope("twitch", "\U0001f3b2 rolls [7] (total 7)")
    _run(dispatch(envelope, {}, http_client=None))

    for _level, message, fields_json in log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_many_dice_show_only_the_total(fake_host) -> None:
    result = _run(transform(_sample_event("!dice 100d1000")))
    assert result is not None
    assert "rolls omitted" in result.payload["text"]
    assert "[" not in result.payload["text"]


def test_bare_dice_is_one_d6(fake_host) -> None:
    result = _run(transform(_sample_event("!dice")))
    assert result is not None
    assert result.payload["text"].startswith("\U0001f3b2 1d6:")


def test_max_bounds_accepted(fake_host) -> None:
    result = _run(transform(_sample_event("!dice 100d1000")))
    assert result is not None
    assert result.payload["text"] != USAGE_TEXT
