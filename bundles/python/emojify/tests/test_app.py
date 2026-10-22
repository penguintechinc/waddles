"""Host-native tests for the `emojify` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (same pattern as `echo`).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from app import (
    _TOO_LONG,
    _UNSAFE_MSG,
    _USAGE,
    MAX_INPUT_LEN,
    apply_transform,
    dispatch,
    transform,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

CASES: list[tuple[str, str]] = [
    ("I love pizza", "I love ❤️ pizza 🍕"),
    ("Dogs, rain!", "Dogs, 🐶 rain! 🌧️"),
    ("plain words", "plain words ✨"),
]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


class _Host:
    """Recorded fake-host state shared with the test body."""

    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.flag_keys: list[str] = []


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Fake WIT host: flag on by default, relay/log recorded."""
    host = _Host()

    def _enabled(key: str, default_value: bool) -> bool:
        host.flag_keys.append(key)
        return host.flag_enabled

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_enabled),
        relay=types.SimpleNamespace(push=lambda p, m: host.relay_calls.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: host.log_calls.append((lvl, msg, fields)),
        ),
        clock=types.SimpleNamespace(
            now_millis=lambda: 0, now_rfc3339=lambda: "", monotonic_nanos=lambda: 0
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return host


def _envelope(
    platform: str, text: str, *, channel_id: str | None = "12345"
) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.emojify",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-21T00:00:00.000Z",
        ),
        ts="2026-10-21T00:00:00.000Z",
    )


@pytest.mark.parametrize(("given", "expected"), CASES)
def test_transform_function_is_deterministic(given: str, expected: str) -> None:
    assert apply_transform(given) == expected
    assert apply_transform(given) == expected


@pytest.mark.parametrize(("given", "expected"), CASES)
def test_command_replies_with_transformed_text(
    given: str, expected: str, fake_host: _Host
) -> None:
    result = _run(transform(_sample_event(f"!emojify {given}")))
    assert result is not None
    assert result.payload["text"] == expected
    assert result.payload["channel_id"] == "12345"


def test_leading_verb_word_is_text_not_a_verb(fake_host: _Host) -> None:
    result = _run(transform(_sample_event("!emojify add the thing")))
    assert result.payload["text"] == apply_transform("add the thing")


@pytest.mark.parametrize("text", ["!emojify", "!emojify   ", "!EMOJIFY"])
def test_empty_input_replies_usage(text: str, fake_host: _Host) -> None:
    assert _run(transform(_sample_event(text))).payload["text"] == _USAGE


def test_length_bound(fake_host: _Host) -> None:
    ok = _run(transform(_sample_event("!emojify " + "a" * MAX_INPUT_LEN)))
    assert ok.payload["text"] != _TOO_LONG
    bad = _run(transform(_sample_event("!emojify " + "a" * (MAX_INPUT_LEN + 1))))
    assert bad.payload["text"] == _TOO_LONG


@pytest.mark.parametrize("lead", ["/", ".", "!"])
def test_refuses_reply_starting_with_chat_command_lead(
    lead: str, fake_host: _Host
) -> None:
    text = lead + "ban"
    result = _run(transform(_sample_event(f"!emojify {text}")))
    assert result.payload["text"] == _UNSAFE_MSG


@pytest.mark.parametrize(
    "text", ["!emojifying", "emojify", "", "hello world", "x !emojify"]
)
def test_non_matching_text_produces_no_reply(text: str, fake_host: _Host) -> None:
    assert _run(transform(_sample_event(text))) is None
    assert fake_host.flag_keys == []


def test_non_chat_payload_is_ignored() -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, fake_host: _Host) -> None:
    result = _run(dispatch(_envelope(platform, "reply"), {}, http_client=None))
    assert result.transport == platform
    provider, message_json = fake_host.relay_calls[0]
    assert provider == platform
    assert json.loads(message_json) == {"channel": "12345", "text": "reply"}


def test_dispatch_raises_when_channel_id_missing(fake_host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(
            dispatch(
                _envelope("twitch", "reply", channel_id=None), {}, http_client=None
            )
        )
    assert fake_host.relay_calls == []


def test_logs_never_contain_text_or_actor(fake_host: _Host) -> None:
    _run(transform(_sample_event("!emojify sensitive-phrase-123")))
    _run(dispatch(_envelope("twitch", "sensitive-phrase-123"), {}, http_client=None))
    assert fake_host.log_calls
    for _lvl, message, fields_json in fake_host.log_calls:
        blob = message + fields_json
        assert "sensitive" not in blob.lower()
        assert "viewer-1" not in blob
        assert "actor" not in json.loads(fields_json)


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"


def test_raw_platform_mention_is_defanged(fake_host: _Host) -> None:
    out = _run(transform(_sample_event("!emojify <@123> hi")))
    assert out is not None
    assert out.payload["text"].startswith("<@" + ZWSP + "123>")


def test_stripped_control_char_cannot_unmask_a_command_lead(fake_host: _Host) -> None:
    """Raw `\x00/x` passes a leading-`/` check, but the host strips NUL afterwards; the refusal
    must therefore be decided on the sanitized text."""
    out = _run(transform(_sample_event("!emojify \x00/x")))
    assert out is not None
    assert out.payload["text"] == _UNSAFE_MSG


def test_mass_mention_is_defanged(fake_host: _Host) -> None:
    out = _run(transform(_sample_event("!emojify @everyone")))
    assert out is not None
    assert "@everyone" not in out.payload["text"]
    assert "@" + ZWSP + "everyone" in out.payload["text"]


def test_host_minted_mention_still_resolves(fake_host: _Host) -> None:
    """The host replaced `@bob` with a minted `{user:..}`; the transform keeps it verbatim."""
    out = _run(transform(_sample_event("!emojify hi {user:tok-1}")))
    assert out is not None
    assert "{user:tok-1}" in out.payload["text"]
    assert "\\{user:tok-1" not in out.payload["text"]


def test_typed_placeholder_stays_escaped(fake_host: _Host) -> None:
    """A typed `{user:..}` arrives host-escaped (`\\{..\\}`) and must never become live."""
    out = _run(transform(_sample_event("!emojify hi \\{user:victim-9\\}")))
    assert out is not None
    assert "\\{user:victim-9\\}" in out.payload["text"]


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(fake_host: _Host) -> None:
    """Activation-gated only: an OFF `waddles.command-emojify` flag must not drop the command."""
    fake_host.flag_enabled = False
    out = _run(transform(_sample_event("!emojify hello")))
    assert out is not None
    assert fake_host.flag_keys == []
