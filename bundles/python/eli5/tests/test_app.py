"""Host-native tests for the `eli5` bundle -- AI is MOCKED via `install_fake_ai_host`."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import app
import pytest
from app import MAX_INPUT_CHARS, PII_REPLY, TOO_LONG, USAGE, dispatch, transform
from waddle_sdk import ai
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import install_fake_ai_host


def _run(coro):
    return asyncio.run(coro)


def _event(text: str, *, platform: str = "twitch") -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="PIIACTOR_alice",
        payload={"text": text, "channel_id": "12345"},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    """Fake AI host + recorded `relay`/`log` imports on one fake `wit_world`."""
    ai_host = install_fake_ai_host(monkeypatch)
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []
    wit = sys.modules["wit_world"]
    wit.imports.relay = types.SimpleNamespace(
        push=lambda p, m: relay_calls.append((p, m))
    )
    wit.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: log_calls.append((lvl, msg, fields)),
    )
    return types.SimpleNamespace(
        ai=ai_host, relay_calls=relay_calls, log_calls=log_calls
    )


def test_prompt_shape_and_ai_options(host) -> None:
    host.ai.reply = "Imagine a super strong vacuum."
    out = _run(transform(_event("!eli5   black holes  ")))
    assert out is not None
    assert out.payload == {
        "text": "Imagine a super strong vacuum.",
        "channel_id": "12345",
    }
    [(prompt, max_tokens, temperature, as_json)] = host.ai.calls
    assert prompt == app.build_prompt("black holes")
    assert "black holes" in prompt
    assert (max_tokens, temperature, as_json) == (
        app.MAX_TOKENS,
        app.TEMPERATURE,
        False,
    )
    assert len(host.ai.calls) == 1


@pytest.mark.parametrize("text", ["!eli5", "!eli5   "])
def test_empty_input_replies_usage_without_ai_call(text: str, host) -> None:
    out = _run(transform(_event(text)))
    assert out is not None and out.payload["text"] == USAGE
    assert host.ai.calls == []


def test_overlong_input_replies_without_ai_call(host) -> None:
    out = _run(transform(_event("!eli5 " + "x" * (MAX_INPUT_CHARS + 1))))
    assert out is not None and out.payload["text"] == TOO_LONG
    assert host.ai.calls == []


@pytest.mark.parametrize("text", ["!eli5ing x", "eli5 x", "hello", ""])
def test_non_matching_text_is_ignored(text: str, host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.ai.calls == []


def test_non_chat_payload_is_ignored(host) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_pii_rejection_becomes_friendly_reply(host) -> None:
    out = _run(transform(_event("!eli5 mail me at a@b.com")))
    assert out is not None and out.payload["text"] == PII_REPLY
    assert "personal info" in PII_REPLY
    assert host.ai.calls == []


@pytest.mark.parametrize(
    ("setup", "exc"),
    [
        (lambda h: setattr(h, "granted", False), ai.DeniedError),
        (lambda h: setattr(h, "rate_limit", 0), ai.DeniedError),
        (lambda h: setattr(h, "reply", ""), ai.ContentFilteredError),
    ],
)
def test_ai_errors_fail_loud(setup, exc, host) -> None:
    setup(host.ai)
    with pytest.raises(exc):
        _run(transform(_event("!eli5 black holes")))


def test_reply_is_whitespace_collapsed_and_capped(host) -> None:
    host.ai.reply = "line one\n\nline   two " + "y" * 1000
    out = _run(transform(_event("!eli5 black holes")))
    assert out is not None
    assert (
        "\n" not in out.payload["text"]
        and len(out.payload["text"]) == app.MAX_REPLY_CHARS
    )


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, host) -> None:
    envelope = StageEnvelope(
        tenant="t",
        community=None,
        app_id="waddles.core.example.eli5",
        stage="action",
        event=_event("Imagine a super strong vacuum.", platform=platform),
        ts="2026-10-21T00:00:00.000Z",
    )
    assert _run(dispatch(envelope, {}, http_client=None)).transport == platform
    provider, msg = host.relay_calls[0]
    assert provider == platform and json.loads(msg) == {
        "channel": "12345",
        "text": "Imagine a super strong vacuum.",
    }


def test_dispatch_requires_channel_id(host) -> None:
    event = _event("x")
    event.payload["channel_id"] = None
    envelope = StageEnvelope(
        tenant="t", community=None, app_id="a", stage="action", event=event, ts="t"
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


# regression: gh-674 -- bundles never log raw user input, actor, or AI output.
def test_logs_are_pii_free(host) -> None:
    host.ai.reply = "PIIOUTPUT_secret answer"
    for text in (
        "!eli5 PIIINPUT_secret",
        "!eli5",
        "!eli5 a@b.com",
        "!eli5 " + "z" * 300,
    ):
        out = _run(transform(_event(text)))
        assert out is not None
        envelope = StageEnvelope(
            tenant="t", community=None, app_id="a", stage="action", event=out, ts="t"
        )
        _run(dispatch(envelope, {}, http_client=None))
    host.ai.granted = False
    with pytest.raises(ai.DeniedError):
        _run(transform(_event("!eli5 PIIINPUT_secret")))

    assert len(host.log_calls) >= 8, "too few log calls -- PII check would be vacuous"
    for _lvl, message, fields_json in host.log_calls:
        blob = f"{message} {fields_json}".lower()
        for secret in (
            "piiactor_alice",
            "piiinput_secret",
            "pioutput_secret",
            "piioutput_secret",
        ):
            assert secret not in blob


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"
FORGED = "ok {user:victim-9} @everyone <@123> @here"
NEUTRALIZED = (
    "ok \\{user:victim-9\\} @" + ZWSP + "everyone <@" + ZWSP + "123> @" + ZWSP + "here"
)


def test_model_output_cannot_forge_mentions(host) -> None:
    """LLM01: a prompt-injected completion must not carry a live placeholder or mass mention."""
    host.ai.reply = FORGED
    out = _run(transform(_event("!eli5 gravity")))
    assert out is not None
    assert out.payload["text"] == NEUTRALIZED


def test_typed_mention_does_not_whitelist_model_output(host) -> None:
    host.ai.reply = "hi {user:tok-1}"
    out = _run(transform(_event("!eli5 gravity {user:tok-1}")))
    assert out is not None
    assert out.payload["text"] == "hi \\{user:tok-1\\}"


@pytest.mark.parametrize(
    "completion",
    [
        "/ban victim",
        ".timeout victim 600",
        "!gamble 1000",
        " /ban victim",
        "\x00/ban victim",  # control char stripped by the sanitizer, then `/` leads
        "\u200b/ban victim",  # invisible lead the host does not strip
        "\u2060.timeout victim 600",
    ],
)
def test_model_completion_can_never_start_with_a_chat_command(
    completion: str, host
) -> None:
    """LLM01: a model reply of `/ban x` must never reach chat as a runnable Twitch/IRC command."""
    host.ai.reply = completion
    out = _run(transform(_event("!eli5 a thing")))
    assert out is not None
    text = out.payload["text"]
    visible = "".join(
        c for c in text if c.isprintable() and c not in "\u200b\u2060"
    ).lstrip()
    assert not visible.startswith(("/", ".", "!"))
    assert text.startswith("\U0001f4ac ")
    assert ("victim" in text) or ("gamble" in text)  # the answer text itself is kept


def test_benign_completions_are_not_prefixed(host) -> None:
    host.ai.reply = "Because of Rayleigh scattering."
    out = _run(transform(_event("!eli5 why is the sky blue")))
    assert out is not None and out.payload["text"] == "Because of Rayleigh scattering."
