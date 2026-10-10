"""Host-native tests for the `boop` bundle's `transform`/`dispatch` logic.

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

from app import _SOLO_BOOPS, _TARGETED_BOOPS, _USAGE, dispatch, transform
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


def test_bare_boop_posts_a_solo_flavor_text(fake_host) -> None:
    result = _run(transform(_sample_event("!boop")))
    assert result is not None
    assert result.payload["text"] in _SOLO_BOOPS


def test_boop_with_valid_target_posts_a_targeted_flavor_text(fake_host) -> None:
    result = _run(transform(_sample_event("!boop someviewer")))
    assert result is not None
    assert any(result.payload["text"] == t.format(target="someviewer") for t in _TARGETED_BOOPS)


def test_boop_with_at_prefixed_target_strips_the_at(fake_host) -> None:
    result = _run(transform(_sample_event("!boop @someviewer")))
    assert result is not None
    assert "someviewer" in result.payload["text"]
    assert "@" not in result.payload["text"]


def test_boop_with_invalid_target_shape_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!boop !!!bad")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_boop_with_bare_at_sign_replies_usage(fake_host) -> None:
    """An `@` alone normalizes to an empty candidate -- must reject, not crash."""
    result = _run(transform(_sample_event("!boop @")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_boop_with_multiple_tokens_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!boop two words here")))
    assert result is not None
    assert result.payload["text"] == _USAGE


@pytest.mark.parametrize("text", ["!boopping", "boop", "!boops", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """`waddles.command-boop` OFF -> a matching command still produces no reply."""
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!boop"))) is None


def _sample_envelope(platform: str, text: str, *, channel_id: str | None = "12345") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.boop",
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
    envelope = _sample_envelope(platform, "Boop reply.")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.transport == platform
    provider, message_json = fake_host.relay_calls[0]
    assert provider == platform
    assert json.loads(message_json) == {
        "channel": "12345",
        "text": "Boop reply.",
    }


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _sample_envelope("twitch", "some boop", channel_id=None)

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.relay_calls == []


def test_transform_and_dispatch_never_log_the_raw_target_or_actor(fake_host) -> None:
    _run(transform(_sample_event("!boop secretviewer")))
    envelope = _sample_envelope("twitch", "some boop")
    _run(dispatch(envelope, {}, http_client=None))

    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "secretviewer" not in message
        assert "secretviewer" not in fields_json
        fields = json.loads(fields_json)
        assert "actor" not in fields
        assert "target" not in fields


# regression: gh-674 -- bundles must never log raw user input (typed tokens) or raw identity,
# on ANY branch (solo / targeted / extra-token usage / bad-shape usage).
def test_logs_never_contain_actor_target_or_extra_tokens(fake_host) -> None:
    for text in (
        "!boop",
        "!boop @PIITARGET_bob",
        "!boop PIITARGET_bob PIIEXTRA_tokens",
        "!boop !!!PIITARGET_bob",
    ):
        event = _sample_event(text)
        event.actor = "PIIACTOR_alice"
        out = _run(transform(event))
        assert out is not None
        envelope = _sample_envelope("twitch", out.payload["text"])
        envelope.event.actor = "PIIACTOR_alice"
        _run(dispatch(envelope, {}, http_client=None))

    assert len(fake_host.log_calls) >= 8, "no/too few log calls -- the PII check would be vacuous"
    for _level, message, fields_json in fake_host.log_calls:
        blob = f"{message} {fields_json}".lower()
        for sentinel in ("piiactor_alice", "piitarget_bob", "piiextra_tokens"):
            assert sentinel not in blob


def test_flag_is_queried_default_off_so_it_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundle must ask the host for `waddles.command-boop` with `default_value=False`."""
    asked: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        asked.append((key, default_value))
        return default_value

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_enabled)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!boop"))) is None
    assert asked == [("waddles.command-boop", False)]


def test_target_with_leading_at_and_dots_hyphens_is_accepted(fake_host) -> None:
    result = _run(transform(_sample_event("!boop @some.viewer-1")))
    assert result is not None
    assert "some.viewer-1" in result.payload["text"]


def test_overlong_target_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!boop " + "x" * 33)))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_dispatch_preserves_reply_text_verbatim(fake_host) -> None:
    """`dispatch` is a pure relay -- it never rewrites what `transform()` built."""
    envelope = _sample_envelope("discord", "exactly this text")
    _run(dispatch(envelope, {}, http_client=None))
    assert json.loads(fake_host.relay_calls[0][1])["text"] == "exactly this text"
