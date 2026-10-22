"""Host-native tests for `translate` against a MOCKED AI host (full e2e needs the Ollama server)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app import MAX_REPLY_LEN, MAX_TEXT_LEN, build_prompt, dispatch, transform
from conftest import MockHost
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SECRET_TEXT = "my zebra password is hunter2-xyzzy"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(text: str, platform: str = "twitch") -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": "12345"},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _envelope(reply: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.translate",
        stage="action",
        event=reply,
        ts="2026-10-21T00:00:00.000Z",
    )


def test_translates_via_ai_host_with_expected_prompt_and_options(
    host: MockHost,
) -> None:
    host.ai.reply = "buenos dias"
    out = _run(transform(_event("!translate Spanish good morning")))
    assert out is not None
    assert out.payload["text"] == "[spanish] buenos dias"
    assert out.payload["command"] == "translate"
    prompt, max_tokens, temperature, json_flag = host.ai.calls[0]
    assert prompt == build_prompt("spanish", "good morning")
    assert "translate it into spanish" in prompt and "ONLY the translation" in prompt
    assert "<text>good morning</text>" in prompt
    assert (
        json_flag is False
    )  # plain-text => host's gemma4:e2b model, never the json model
    assert max_tokens == 256 and temperature == 0.2


@pytest.mark.parametrize("lang", ["es", "pt-br", "JA", "zh_hans"])
def test_target_lang_forms_accepted(lang: str, host: MockHost) -> None:
    out = _run(transform(_event(f"!translate {lang} hello")))
    assert out.payload["command"] == "translate"
    assert f"into {lang.lower()}" in host.ai.calls[0][0]


@pytest.mark.parametrize(
    "text",
    [
        "!translate",
        "!translate es",
        "!translate   es   ",
        "!translate 123 hello",
        "!translate e hello",
        "!translate es;ignore-rules hello",
        "!translate list",
        "!translate set es hello",
    ],
)
def test_missing_or_invalid_input_replies_usage_without_calling_ai(
    text: str, host: MockHost
) -> None:
    out = _run(transform(_event(text)))
    assert out.payload["command"] == "usage"
    assert out.payload["text"]
    assert host.ai.calls == []


def test_overlong_input_is_bounded_before_ai_call(host: MockHost) -> None:
    out = _run(transform(_event("!translate es " + "a" * (MAX_TEXT_LEN + 1))))
    assert out.payload["command"] == "usage"
    assert str(MAX_TEXT_LEN) in out.payload["text"]
    assert host.ai.calls == []
    ok = _run(transform(_event("!translate es " + "a" * MAX_TEXT_LEN)))
    assert ok.payload["command"] == "translate"


def test_reply_is_one_bounded_line(host: MockHost) -> None:
    host.ai.reply = "line one\n\nline two " + "x" * 2000
    out = _run(transform(_event("!translate fr hi")))
    text = out.payload["text"]
    assert "\n" not in text and len(text) <= len("[fr] ") + MAX_REPLY_LEN
    assert text.startswith("[fr] line one line two")


@pytest.mark.parametrize(
    "bad",
    ["!translate es mail me at bob@example.com", "!translate es hi @someone"],
)
def test_pii_in_text_is_rejected_loud_not_translated(bad: str, host: MockHost) -> None:
    out = _run(transform(_event(bad)))
    assert out.payload["command"] == "error"
    assert "personal info" in out.payload["text"]
    assert host.ai.calls == []


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        ("granted", "not enabled"),
        ("rate", "busy"),
        ("empty", "couldn't produce"),
    ],
)
def test_ai_failures_fail_loud(setup: str, needle: str, host: MockHost) -> None:
    if setup == "granted":
        host.ai.granted = False
    elif setup == "rate":
        host.ai.rate_limit = 0
    else:
        host.ai.reply = ""
    out = _run(transform(_event("!translate de hello there")))
    assert out.payload["command"] == "error"
    assert needle in out.payload["text"]
    # dispatch relays the clear message, then raises (never a silent success).
    with pytest.raises(RuntimeError, match="translate failed"):
        _run(dispatch(_envelope(out), {}, http_client=None))
    assert host.relays and needle in host.relays[0][1]["text"]


def test_unavailable_model_fails_loud(
    host: MockHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app
    from waddle_sdk import ai

    async def boom(*_a: Any, **_k: Any) -> str:
        raise ai.UnavailableError(
            "ai.generate failed: Error_Unavailable: secret-internal-detail"
        )

    monkeypatch.setattr(app.ai, "generate", boom)
    out = _run(transform(_event("!translate de hello")))
    assert (
        out.payload["command"] == "error"
        and "unavailable" in out.payload["text"].lower()
    )
    assert "secret-internal-detail" not in out.payload["text"] + host.log_text()


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, host: MockHost) -> None:
    out = _run(transform(_event("!translate es hello", platform)))
    result = _run(dispatch(_envelope(out), {}, http_client=None))
    assert result.transport == platform
    assert host.relays == [
        (platform, {"channel": "12345", "text": "[es] a generated line"})
    ]


def test_dispatch_requires_channel_id(host: MockHost) -> None:
    ev = _event("x")
    ev.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(ev), {}, http_client=None))


def test_logs_are_pii_free(host: MockHost) -> None:
    host.ai.reply = "SECRET-TRANSLATION-OUTPUT"
    _run(transform(_event(f"!translate es {SECRET_TEXT}")))
    out = _run(transform(_event("!translate es mail bob@example.com")))
    host.ai.granted = False
    bad = _run(transform(_event(f"!translate es {SECRET_TEXT}")))
    for ev in (bad, out):
        try:
            _run(dispatch(_envelope(ev), {}, http_client=None))
        except RuntimeError:
            pass
    logged = host.log_text()
    assert host.logs
    for needle in (
        "hunter2",
        "zebra",
        "SECRET-TRANSLATION-OUTPUT",
        "viewer-1",
        "bob@",
        "example",
    ):
        assert needle not in logged
    for _l, _m, fields in host.logs:
        assert "actor" not in json.loads(fields)
    assert "lang=" in logged or "lang" in logged and "input_len" in logged


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"
FORGED = "ok {user:victim-9} @everyone <@123> @here"
NEUTRALIZED = (
    "ok \\{user:victim-9\\} @" + ZWSP + "everyone <@" + ZWSP + "123> @" + ZWSP + "here"
)


def test_model_output_cannot_forge_mentions(host: MockHost) -> None:
    """LLM01: a prompt-injected completion must not carry a live placeholder or mass mention."""
    host.ai.reply = FORGED
    out = _run(transform(_event("!translate es hello")))
    assert out is not None
    assert out.payload["text"] == "[es] " + NEUTRALIZED


def test_typed_mention_does_not_whitelist_model_output(host: MockHost) -> None:
    """A mention the user typed must not make the model's echo of it live (no allow_tokens)."""
    host.ai.reply = "hi {user:tok-1}"
    out = _run(transform(_event("!translate es hello {user:tok-1}")))
    assert out is not None
    assert out.payload["text"] == "[es] " + "hi \\{user:tok-1\\}"


def test_usage_echo_of_typed_language_is_neutralized(host: MockHost) -> None:
    out = _run(transform(_event("!translate @everyone hello")))
    assert out is not None
    assert out.payload["command"] == "usage"
    assert out.payload["text"].startswith(
        "'@" + ZWSP + "everyone' is not a valid language."
    )
    forged = _run(transform(_event("!translate {user:victim-9} hello")))
    assert forged is not None
    assert forged.payload["text"].startswith(
        "'\\{user:victim-9\\}' is not a valid language."
    )


def test_non_matching_events_are_ignored(host: MockHost) -> None:
    assert _run(transform(_event("!translator es hi"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.ai.calls == []


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(host: MockHost) -> None:
    """Activation-gated only: an OFF `waddles.command-translate` flag must not drop the command."""
    host.flag_enabled = False
    out = _run(transform(_event("!translate")))
    assert out is not None
