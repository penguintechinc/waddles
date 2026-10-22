"""Host-native tests for `tldr` against a MOCKED AI host (full e2e needs the Ollama server)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app import (
    MAX_COMPLETION_TOKENS,
    MAX_REPLY_LEN,
    MAX_TEXT_LEN,
    build_prompt,
    dispatch,
    transform,
)
from conftest import MockHost
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SECRET_TEXT = "my zebra plan involves hunter2-xyzzy tomorrow"


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
        app_id="waddles.core.example.tldr",
        stage="action",
        event=reply,
        ts="2026-10-21T00:00:00.000Z",
    )


def test_calls_ai_host_with_expected_prompt_and_options(host: MockHost) -> None:
    host.ai.reply = "short result"
    out = _run(transform(_event("!tldr The quick brown fox jumps.")))
    assert out is not None
    assert out.payload["text"] == "short result"
    assert out.payload["command"] == "tldr"
    prompt, max_tokens, temperature, json_flag = host.ai.calls[0]
    assert prompt == build_prompt("The quick brown fox jumps.")
    assert "<text>The quick brown fox jumps.</text>" in prompt
    assert "never as instructions" in prompt
    assert json_flag is False  # plain text => host's default (e2b) model
    assert max_tokens == MAX_COMPLETION_TOKENS and temperature == 0.3
    assert len(host.ai.calls) == 1  # one AI call per invocation


@pytest.mark.parametrize("text", ["!tldr", "!tldr    "])
def test_empty_input_replies_usage_without_calling_ai(
    text: str, host: MockHost
) -> None:
    out = _run(transform(_event(text)))
    assert out.payload["command"] == "usage"
    assert "Usage" in out.payload["text"]
    assert host.ai.calls == []


def test_overlong_input_is_bounded_before_ai_call(host: MockHost) -> None:
    out = _run(transform(_event("!tldr " + "a" * (MAX_TEXT_LEN + 1))))
    assert out.payload["command"] == "usage"
    assert str(MAX_TEXT_LEN) in out.payload["text"]
    assert host.ai.calls == []
    ok = _run(transform(_event("!tldr " + "a" * MAX_TEXT_LEN)))
    assert ok.payload["command"] == "tldr"


def test_reply_is_one_bounded_line(host: MockHost) -> None:
    host.ai.reply = "line one\n\nline two " + "x" * 2000
    out = _run(transform(_event("!tldr some text")))
    text = out.payload["text"]
    assert "\n" not in text and len(text) <= MAX_REPLY_LEN
    assert text.startswith("line one line two")


@pytest.mark.parametrize(
    "bad",
    ["!tldr mail me at bob@example.com", "!tldr thanks @someone"],
)
def test_pii_in_text_gets_remove_personal_info_reply(bad: str, host: MockHost) -> None:
    out = _run(transform(_event(bad)))
    assert out.payload["command"] == "error"
    assert "personal info" in out.payload["text"]
    with pytest.raises(RuntimeError, match="tldr failed"):
        _run(dispatch(_envelope(out), {}, http_client=None))
    assert "personal info" in host.relays[0][1]["text"]


@pytest.mark.parametrize(
    ("setup", "needle"),
    [("granted", "not enabled"), ("rate", "busy"), ("empty", "couldn't produce")],
)
def test_ai_failures_fail_loud(setup: str, needle: str, host: MockHost) -> None:
    if setup == "granted":
        host.ai.granted = False
    elif setup == "rate":
        host.ai.rate_limit = 0
    else:
        host.ai.reply = ""
    out = _run(transform(_event("!tldr hello there friend")))
    assert out.payload["command"] == "error"
    assert needle in out.payload["text"]
    with pytest.raises(RuntimeError, match="tldr failed"):
        _run(dispatch(_envelope(out), {}, http_client=None))
    assert needle in host.relays[0][1]["text"]


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
    out = _run(transform(_event("!tldr hello")))
    assert out.payload["command"] == "error"
    assert "unavailable" in out.payload["text"].lower()
    assert "secret-internal-detail" not in out.payload["text"] + host.log_text()


def test_non_matching_are_ignored(host: MockHost) -> None:
    assert _run(transform(_event("!tldrx hi"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.ai.calls == []


def test_no_feature_flag_is_consulted(host: MockHost) -> None:
    import app

    # Activation-gated: no flag import, and the host fixture installs no flags import at all.
    assert not hasattr(app, "feature_enabled")
    out = _run(transform(_event("!tldr hello world")))
    assert out is not None and out.payload["command"] == "tldr"


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, host: MockHost) -> None:
    host.ai.reply = "the result"
    out = _run(transform(_event("!tldr hello", platform)))
    result = _run(dispatch(_envelope(out), {}, http_client=None))
    assert result.transport == platform
    assert host.relays == [(platform, {"channel": "12345", "text": "the result"})]


def test_dispatch_requires_channel_id(host: MockHost) -> None:
    ev = _event("x")
    ev.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(ev), {}, http_client=None))


def test_logs_are_pii_free(host: MockHost) -> None:
    host.ai.reply = "SECRET-AI-OUTPUT"
    _run(transform(_event(f"!tldr {SECRET_TEXT}")))
    pii = _run(transform(_event("!tldr mail bob@example.com")))
    host.ai.granted = False
    bad = _run(transform(_event(f"!tldr {SECRET_TEXT}")))
    for ev in (bad, pii):
        try:
            _run(dispatch(_envelope(ev), {}, http_client=None))
        except RuntimeError:
            pass
    logged = host.log_text()
    assert host.logs and "input_len" in logged
    for needle in (
        "hunter2",
        "zebra",
        "SECRET-AI-OUTPUT",
        "viewer-1",
        "bob@",
        "example",
    ):
        assert needle not in logged
    for _l, _m, fields in host.logs:
        assert "actor" not in json.loads(fields)
