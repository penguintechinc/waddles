"""Host-native tests for the `eightball` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- same pattern as `bundles/python/pyping/tests/test_app.py`: a fake
`wit_world` module stands in for the WIT host imports (`flags`, `log`, `relay`), isolating
"does this bundle build the right calls" from "does the WIT import actually work" (already
covered by `waddle_sdk`'s own tests).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import ANSWERS, dispatch, transform
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


def test_eightball_without_a_question_still_produces_an_answer(fake_host) -> None:
    result = _run(transform(_sample_event("!8ball")))
    assert result is not None
    assert any(result.payload["text"].endswith(answer) for answer in ANSWERS)


def test_eightball_with_a_question_produces_an_answer(fake_host) -> None:
    result = _run(transform(_sample_event("!8ball will it rain tomorrow?")))
    assert result is not None
    assert result.platform == "twitch"
    assert result.payload["channel_id"] == "12345"


@pytest.mark.parametrize("text", ["!8balling", "8ball", "!eightball", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """`waddles.command-8ball` OFF -> a matching command still produces no reply."""
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!8ball"))) is None


def _sample_envelope(platform: str, text: str, *, channel_id: str | None = "12345") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.eightball",
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
def test_dispatch_relays_to_the_events_own_origin_platform(platform: str, fake_host) -> None:
    envelope = _sample_envelope(platform, "\U0001f3b1 It is certain.")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.transport == platform
    provider, message_json = fake_host.relay_calls[0]
    assert provider == platform
    assert json.loads(message_json) == {"channel": "12345", "text": "\U0001f3b1 It is certain."}


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _sample_envelope("twitch", "some answer", channel_id=None)

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.relay_calls == []


def test_transform_and_dispatch_never_log_the_raw_actor(fake_host) -> None:
    _run(transform(_sample_event("!8ball")))
    envelope = _sample_envelope("twitch", "\U0001f3b1 It is certain.")
    _run(dispatch(envelope, {}, http_client=None))

    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


# regression: gh-674 -- bundles must never log raw user input (the question text) or raw identity.
def test_logs_never_contain_actor_or_question_text(fake_host) -> None:
    for text in ("!8ball", "!8ball will PIIQUESTION_secret happen?", "!8BALL   PIIQUESTION_secret"):
        event = _sample_event(text)
        event.actor = "PIIACTOR_alice"
        out = _run(transform(event))
        assert out is not None
        envelope = _sample_envelope("twitch", out.payload["text"])
        envelope.event.actor = "PIIACTOR_alice"
        _run(dispatch(envelope, {}, http_client=None))

    assert len(fake_host.log_calls) >= 6, "too few log calls -- PII check would be vacuous"
    for _level, message, fields_json in fake_host.log_calls:
        blob = f"{message} {fields_json}".lower()
        assert "piiactor_alice" not in blob
        assert "piiquestion_secret" not in blob


def test_flag_is_queried_default_off_so_it_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundle must ask the host for `waddles.command-8ball` with `default_value=False`."""
    asked: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        asked.append((key, default_value))
        return default_value

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_enabled)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!8ball"))) is None
    assert asked == [("waddles.command-8ball", False)]


def test_unmatched_text_never_consults_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cheap-skip ordering: a non-command message must not pay a flag host round trip."""
    asked: list[str] = []
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda key, default_value: asked.append(key) or True)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("hello there"))) is None
    assert asked == []


@pytest.mark.parametrize("answer", ANSWERS)
def test_every_canned_answer_is_reachable_and_emoji_prefixed(
    answer: str, monkeypatch: pytest.MonkeyPatch, fake_host
) -> None:
    import app

    monkeypatch.setattr(app.random, "choice", lambda _seq: answer)
    result = _run(transform(_sample_event("!8ball ready?")))
    assert result is not None
    assert result.payload["text"] == f"\U0001f3b1 {answer}"


def test_dispatch_rejects_empty_channel_id_string(fake_host) -> None:
    envelope = _sample_envelope("twitch", "x", channel_id="")
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.relay_calls == []
