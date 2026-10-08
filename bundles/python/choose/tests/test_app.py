"""Host-native tests for the `choose` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- same pattern as `bundles/python/eightball/tests/
test_app.py`: a fake `wit_world` module stands in for the WIT host imports
(`flags`, `log`, `relay`), isolating "does this bundle build the right
calls" from "does the WIT import actually work" (already covered by
`waddle_sdk`'s own tests).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import MAX_OPTION_LEN, MAX_OPTIONS, _USAGE, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-08T00:00:00.000Z",
    )


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: `flags.enabled` always True, `relay.push`/`log.write` recorded."""
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    relay_mod = types.SimpleNamespace(push=lambda provider, msg: relay_calls.append((provider, msg)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: log_calls.append((lvl, msg, fields)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(relay_calls=relay_calls, log_calls=log_calls)


def test_pipe_separated_options_pick_one_of_the_options(fake_host) -> None:
    result = _run(transform(_sample_event("!choose pizza night | movie night | game night")))
    assert result is not None
    assert result.payload["text"] in ("pizza night", "movie night", "game night")


def test_space_separated_options_pick_one_of_the_options(fake_host) -> None:
    result = _run(transform(_sample_event("!choose heads tails")))
    assert result is not None
    assert result.payload["text"] in ("heads", "tails")


def test_bare_choose_with_no_options_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!choose")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_single_option_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!choose onlyoption")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_too_many_options_replies_bounds_error(fake_host) -> None:
    options = " ".join(f"opt{i}" for i in range(MAX_OPTIONS + 1))
    result = _run(transform(_sample_event(f"!choose {options}")))
    assert result is not None
    assert "too many options" in result.payload["text"]


def test_one_option_too_long_replies_bounds_error(fake_host) -> None:
    long_opt = "x" * (MAX_OPTION_LEN + 1)
    result = _run(transform(_sample_event(f"!choose a | {long_opt}")))
    assert result is not None
    assert "too long" in result.payload["text"]


@pytest.mark.parametrize("text", ["!choosing a b", "choose a b", "!pick a b", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """`waddles.command-choose` OFF -> a matching command still produces no reply."""
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!choose a b"))) is None


def _sample_envelope(platform: str, text: str, *, channel_id: str | None = "12345") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.choose",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-08T00:00:00.000Z",
        ),
        ts="2026-10-08T00:00:00.000Z",
    )


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_the_events_own_origin_platform(platform: str, fake_host) -> None:
    envelope = _sample_envelope(platform, "pizza night")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.transport == platform
    provider, message_json = fake_host.relay_calls[0]
    assert provider == platform
    assert json.loads(message_json) == {"channel": "12345", "text": "pizza night"}


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _sample_envelope("twitch", "pizza night", channel_id=None)

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.relay_calls == []


def test_transform_and_dispatch_never_log_the_raw_option_text_or_actor(fake_host) -> None:
    _run(transform(_sample_event("!choose secret-option-a secret-option-b")))
    envelope = _sample_envelope("twitch", "secret-option-a")
    _run(dispatch(envelope, {}, http_client=None))

    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "secret-option" not in message
        assert "secret-option" not in fields_json
        fields = json.loads(fields_json)
        assert "actor" not in fields
        assert "text" not in fields
