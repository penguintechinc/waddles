"""Host-native tests for `sentiment` against a MOCKED AI host (full e2e needs the Ollama server)."""

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
        app_id="waddles.core.example.sentiment",
        stage="action",
        event=reply,
        ts="2026-10-21T00:00:00.000Z",
    )


GOOD = '{"sentiment": "positive", "confidence": 0.92, "emoji": "\U0001f604"}'


def test_analyzes_via_ai_host_with_json_true_and_expected_prompt(
    host: MockHost,
) -> None:
    host.ai.reply = GOOD
    out = _run(transform(_event("!sentiment I love this stream")))
    assert out.payload["command"] == "sentiment"
    assert out.payload["text"] == "\U0001f604 Sentiment: positive (92% confident)"
    assert len(host.ai.calls) == 1  # one AI call per invocation
    prompt, max_tokens, temperature, json_flag = host.ai.calls[0]
    assert prompt == build_prompt("I love this stream")
    assert (
        "ONLY a JSON object" in prompt and "<text>I love this stream</text>" in prompt
    )
    assert json_flag is True  # structured => host's gemma e4b
    assert max_tokens == 128 and temperature == 0.1


def test_fenced_json_is_tolerated(host: MockHost) -> None:
    host.ai.reply = "```json\n" + GOOD + "\n```"
    assert _run(transform(_event("!sentiment fine"))).payload["command"] == "sentiment"


def test_untrusted_emoji_is_replaced_by_label_emoji(host: MockHost) -> None:
    host.ai.reply = (
        '{"sentiment": "Negative", "confidence": 1, "emoji": "<script>x</script>"}'
    )
    out = _run(transform(_event("!sentiment ugh")))
    assert out.payload["text"] == "\U0001f61e Sentiment: negative (100% confident)"


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        "[1, 2]",
        "{}",
        '{"sentiment": "ecstatic", "confidence": 0.5, "emoji": "x"}',
        '{"sentiment": "positive", "confidence": 1.5, "emoji": "x"}',
        '{"sentiment": "positive", "confidence": true, "emoji": "x"}',
        '{"sentiment": "positive", "confidence": "high", "emoji": "x"}',
        '{"sentiment": "positive", "confidence": 0.5}',
        '{"sentiment": "positive", "confidence": 0.5, "emoji": 7}',
        '{"sentiment": "positive", "confidence": 0.5, "emoji": "x"',
    ],
)
def test_malformed_json_fails_loud_without_crashing(bad: str, host: MockHost) -> None:
    host.ai.reply = bad
    out = _run(transform(_event("!sentiment hello there")))  # must not raise
    assert out.payload["command"] == "error"
    assert "unreadable analysis" in out.payload["text"]
    assert bad not in out.payload["text"]
    with pytest.raises(RuntimeError, match="sentiment failed"):
        _run(dispatch(_envelope(out), {}, http_client=None))
    assert host.relays and "unreadable" in host.relays[0][1]["text"]


def test_logs_are_pii_free(host: MockHost) -> None:
    host.ai.reply = GOOD
    ok = _run(transform(_event(f"!sentiment {SECRET_TEXT}")))
    host.ai.reply = "SECRET-MODEL-OUTPUT-zzz"
    malformed = _run(transform(_event(f"!sentiment {SECRET_TEXT}")))
    pii = _run(transform(_event("!sentiment mail bob@example.com")))
    for ev in (ok, malformed, pii):
        try:
            _run(dispatch(_envelope(ev), {}, http_client=None))
        except RuntimeError:
            pass
    logged = host.log_text()
    assert host.logs and "sentiment" in logged and "input_len" in logged
    for needle in (
        "hunter2",
        "zebra",
        "SECRET-MODEL-OUTPUT",
        "viewer-1",
        "bob@",
        "example",
    ):
        assert needle not in logged
    for _l, _m, fields in host.logs:
        assert "actor" not in json.loads(fields)


@pytest.mark.parametrize("text", ["!sentiment", "!sentiment   "])
def test_missing_input_replies_usage_without_calling_ai(
    text: str, host: MockHost
) -> None:
    out = _run(transform(_event(text)))
    assert out.payload["command"] == "usage" and out.payload["text"]
    assert host.ai.calls == []


def test_overlong_input_is_bounded_before_ai_call(host: MockHost) -> None:
    out = _run(transform(_event("!sentiment " + "a" * (MAX_TEXT_LEN + 1))))
    assert (
        out.payload["command"] == "usage" and str(MAX_TEXT_LEN) in out.payload["text"]
    )
    assert host.ai.calls == []


def test_non_matching_is_ignored(host: MockHost) -> None:
    assert _run(transform(_event("!sentimentx hi"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.ai.calls == []


@pytest.mark.parametrize(
    "bad", ["!sentiment mail me at bob@example.com", "!sentiment hi @someone"]
)
def test_pii_in_text_is_rejected_loud(bad: str, host: MockHost) -> None:
    out = _run(transform(_event(bad)))
    assert out.payload["command"] == "error"
    assert "personal info" in out.payload["text"]
    assert host.ai.calls == []
    with pytest.raises(RuntimeError, match="sentiment failed"):
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
    out = _run(transform(_event("!sentiment hello there")))
    assert out.payload["command"] == "error" and needle in out.payload["text"]
    with pytest.raises(RuntimeError, match="sentiment failed"):
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
    out = _run(transform(_event("!sentiment hello")))
    assert (
        out.payload["command"] == "error"
        and "unavailable" in out.payload["text"].lower()
    )
    assert "secret-internal-detail" not in out.payload["text"] + host.log_text()


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, host: MockHost) -> None:
    host.ai.reply = GOOD
    out = _run(transform(_event("!sentiment hello", platform)))
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


def test_model_supplied_emoji_cannot_carry_a_mention(host: MockHost) -> None:
    """The emoji only has to be short/printable/not-all-ASCII, so `@here` + emoji passes parsing."""
    host.ai.reply = json.dumps(
        {"sentiment": "positive", "confidence": 0.9, "emoji": "@here\U0001f642"}
    )
    out = _run(transform(_event("!sentiment I love this stream")))
    assert out is not None
    assert out.payload["command"] == "sentiment"
    assert (
        out.payload["text"]
        == "@" + ZWSP + "here\U0001f642 Sentiment: positive (90% confident)"
    )


def test_model_emoji_discord_mention_is_defanged(host: MockHost) -> None:
    host.ai.reply = json.dumps(
        {"sentiment": "negative", "confidence": 0.5, "emoji": "<@1>\U0001f620"}
    )
    out = _run(transform(_event("!sentiment ugh")))
    assert out is not None
    assert out.payload["text"].startswith(
        "<@" + ZWSP + "1>\U0001f620 Sentiment: negative"
    )
