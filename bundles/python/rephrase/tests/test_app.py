"""Host-native tests for `rephrase` against a MOCKED AI host (full e2e needs the Ollama server)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app import MAX_TEXT_LEN, build_prompt, dispatch, transform
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
        app_id="waddles.core.example.rephrase",
        stage="action",
        event=reply,
        ts="2026-10-21T00:00:00.000Z",
    )


def test_rephrases_via_ai_host_with_expected_prompt_and_options(host: MockHost) -> None:
    host.ai.reply = "Could you please give that to me now?"
    out = _run(transform(_event("!rephrase give me that now")))
    assert out.payload["command"] == "rephrase"
    assert "Could you please give that to me now?" in out.payload["text"]
    assert len(host.ai.calls) == 1  # one AI call per invocation
    prompt, max_tokens, temperature, json_flag = host.ai.calls[0]
    assert prompt == build_prompt("give me that now")
    assert (
        "ONLY the reworded text" in prompt and "<text>give me that now</text>" in prompt
    )
    assert "family-friendly" in prompt
    assert json_flag is False  # plain text => host's gemma e2b
    assert max_tokens == 256 and temperature == 0.5


def test_reply_is_one_bounded_line(host: MockHost) -> None:
    host.ai.reply = "line one\n\nline two " + "x" * 2000
    text = _run(transform(_event("!rephrase hi"))).payload["text"]
    assert "\n" not in text and len(text) <= 460 and "line one line two" in text


def test_logs_are_pii_free(host: MockHost) -> None:
    host.ai.reply = "SECRET-REWRITE-OUTPUT"
    ok = _run(transform(_event(f"!rephrase {SECRET_TEXT}")))
    pii = _run(transform(_event("!rephrase mail bob@example.com")))
    host.ai.granted = False
    bad = _run(transform(_event(f"!rephrase {SECRET_TEXT}")))
    for ev in (ok, bad, pii):
        try:
            _run(dispatch(_envelope(ev), {}, http_client=None))
        except RuntimeError:
            pass
    logged = host.log_text()
    assert host.logs and "input_len" in logged
    for needle in (
        "hunter2",
        "zebra",
        "SECRET-REWRITE-OUTPUT",
        "viewer-1",
        "bob@",
        "example",
    ):
        assert needle not in logged
    for _l, _m, fields in host.logs:
        assert "actor" not in json.loads(fields)


@pytest.mark.parametrize("text", ["!rephrase", "!rephrase   "])
def test_missing_input_replies_usage_without_calling_ai(
    text: str, host: MockHost
) -> None:
    out = _run(transform(_event(text)))
    assert out.payload["command"] == "usage" and out.payload["text"]
    assert host.ai.calls == []


def test_overlong_input_is_bounded_before_ai_call(host: MockHost) -> None:
    out = _run(transform(_event("!rephrase " + "a" * (MAX_TEXT_LEN + 1))))
    assert (
        out.payload["command"] == "usage" and str(MAX_TEXT_LEN) in out.payload["text"]
    )
    assert host.ai.calls == []


def test_non_matching_is_ignored(host: MockHost) -> None:
    assert _run(transform(_event("!rephrasex hi"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.ai.calls == []


@pytest.mark.parametrize(
    "bad", ["!rephrase mail me at bob@example.com", "!rephrase hi @someone"]
)
def test_pii_in_text_is_rejected_loud(bad: str, host: MockHost) -> None:
    out = _run(transform(_event(bad)))
    assert out.payload["command"] == "error"
    assert "personal info" in out.payload["text"]
    assert host.ai.calls == []
    with pytest.raises(RuntimeError, match="rephrase failed"):
        _run(dispatch(_envelope(out), {}, http_client=None))


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
    out = _run(transform(_event("!rephrase hello there")))
    assert out.payload["command"] == "error" and needle in out.payload["text"]
    with pytest.raises(RuntimeError, match="rephrase failed"):
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
    out = _run(transform(_event("!rephrase hello")))
    assert (
        out.payload["command"] == "error"
        and "unavailable" in out.payload["text"].lower()
    )
    assert "secret-internal-detail" not in out.payload["text"] + host.log_text()


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, host: MockHost) -> None:
    host.ai.reply = "Please send it."
    out = _run(transform(_event("!rephrase hello", platform)))
    result = _run(dispatch(_envelope(out), {}, http_client=None))
    assert result.transport == platform
    assert host.relays == [
        (platform, {"channel": "12345", "text": out.payload["text"]})
    ]


def test_dispatch_requires_channel_id(host: MockHost) -> None:
    ev = _event("x")
    ev.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(ev), {}, http_client=None))


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"
FORGED = "ok {user:victim-9} @everyone <@123> @here"
NEUTRALIZED = (
    "ok \\{user:victim-9\\} @" + ZWSP + "everyone <@" + ZWSP + "123> @" + ZWSP + "here"
)


def test_model_output_cannot_forge_mentions(host: MockHost) -> None:
    """LLM01: a prompt-injected completion must not carry a live placeholder or mass mention."""
    host.ai.reply = FORGED
    out = _run(transform(_event("!rephrase hello there")))
    assert out is not None
    assert out.payload["text"] == "✍️ " + NEUTRALIZED


def test_typed_mention_does_not_whitelist_model_output(host: MockHost) -> None:
    """A mention the user typed must not make the model's echo of it live (no allow_tokens)."""
    host.ai.reply = "hi {user:tok-1}"
    out = _run(transform(_event("!rephrase hello there {user:tok-1}")))
    assert out is not None
    assert out.payload["text"] == "✍️ " + "hi \\{user:tok-1\\}"


@pytest.mark.parametrize(
    "completion",
    [
        "/ban victim",
        ".timeout victim 600",
        "!gamble 1000",
        " /ban victim",
        "\x00/ban victim",
        "\u200b/ban victim",
    ],
)
def test_model_completion_can_never_lead_the_reply(
    completion: str, host: MockHost
) -> None:
    """LLM01: the fixed `[pencil]` prefix means a completion of `/ban x` never leads the reply."""
    host.ai.reply = completion
    out = _run(transform(_event("!rephrase give me that now")))
    assert out is not None and out.payload["command"] == "rephrase"
    text = out.payload["text"]
    assert text.startswith("\u270d\ufe0f ")
    assert not text.startswith(("/", ".", "!"))
    assert ("victim" in text) or ("gamble" in text)  # the answer text itself is kept
