"""`SummaryService`: plain-text prompts/parsers for text-only models, JSON only for JSON-capable ones.

regression: both summary prompts demanded "JSON format" from EVERY model and the parsers ran
`json.loads()` on the provider's `AIResponse` OBJECT (not its text) -- a TypeError that the
`except json.JSONDecodeError` never caught, so summaries crashed outright; and the prompt's
`json.dumps(messages)` raised on the `datetime` values real DB rows carry.

The provider is the REAL `AIProviderService` over an offline transport; only the DB-facing
methods (`_get_messages_for_period`, `_get_stream_summaries`, `save_insight`) are stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from ai_fakes import FakeConfig

from services import summary_service as summary_module
from services.ai_provider import AIProviderService
from services.summary_service import SummaryService

STREAM_START = datetime(2026, 10, 1, 18, 0, 0)
STREAM_END = STREAM_START + timedelta(minutes=90)

TEXT_REPLY = """\
**TITLE:** Stream Summary - Oct 1, 2026
SUMMARY: Chat celebrated a speedrun PB.
It stayed friendly all night.
KEY TOPICS: speedrun, giveaways , raid
NOTABLE MOMENTS: the PB; the raid at the end
SENTIMENT: positive
"""


class _LogRecorder:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, **kwargs: Any) -> None:
        self.warnings.append(message)

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(summary_module, "logger", recorder)
    return recorder


def _provider(reply: str, wire: list[dict[str, Any]], **cfg: Any) -> AIProviderService:
    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        return httpx.Response(
            200, json={"response": reply, "done": True, "done_reason": "stop", "eval_count": 5}
        )

    provider = AIProviderService(FakeConfig(**cfg))  # type: ignore[arg-type]
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _service(
    provider: AIProviderService, monkeypatch: pytest.MonkeyPatch
) -> tuple[SummaryService, list[dict[str, Any]]]:
    service = SummaryService(ai_provider=provider, mem0_service=None, db_connection=None)
    saved: list[dict[str, Any]] = []

    async def messages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "platform_user_id": "u1",
                "platform_username": "alice",
                "message_content": "speedrun personal best incoming",
                "created_at": STREAM_START,  # a real DB row carries datetimes
            },
            {
                "platform_user_id": "u2",
                "platform_username": "bob",
                "message_content": "giveaways tonight please",
                "created_at": STREAM_START + timedelta(minutes=5),
            },
        ]

    async def stream_summaries(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"metadata": {"key_topics": ["speedrun"], "sentiment": "positive"}}]

    async def save_insight(**kwargs: Any) -> int:
        saved.append(kwargs)
        return 77

    monkeypatch.setattr(service, "_get_messages_for_period", messages)
    monkeypatch.setattr(service, "_get_stream_summaries", stream_summaries)
    monkeypatch.setattr(service, "save_insight", save_insight)
    return service, saved


CONTEXT = {
    "duration_minutes": 90,
    "message_count": 2,
    "messages": [{"message_content": "hi", "created_at": STREAM_START}],
    "stream_count": 1,
    "total_messages": 2,
    "top_chatters": [{"user": "alice", "count": 1}],
    "popular_topics": ["speedrun"],
    "sentiment_trend": "positive",
}


class TestPrompts:
    def test_text_prompts_never_ask_for_json(self) -> None:
        service = SummaryService(None, None, None)
        for prompt in (
            service._build_stream_summary_prompt(CONTEXT, json_mode=False),
            service._build_weekly_summary_prompt(CONTEXT, json_mode=False),
        ):
            assert "JSON" not in prompt
            assert "TITLE:" in prompt and "SUMMARY:" in prompt

    def test_json_prompts_ask_for_json_with_the_documented_shape(self) -> None:
        service = SummaryService(None, None, None)
        stream = service._build_stream_summary_prompt(CONTEXT, json_mode=True)
        weekly = service._build_weekly_summary_prompt(CONTEXT, json_mode=True)
        assert "summary in JSON format" in stream and '"key_topics"' in stream
        assert "summary in JSON format" in weekly and '"title"' in weekly

    def test_default_is_the_text_prompt(self) -> None:
        service = SummaryService(None, None, None)
        assert "JSON" not in service._build_stream_summary_prompt(CONTEXT)

    def test_datetimes_in_db_rows_do_not_break_the_prompt(self) -> None:
        service = SummaryService(None, None, None)
        prompt = service._build_stream_summary_prompt(CONTEXT, json_mode=False)
        assert "2026-10-01 18:00:00" in prompt

    def test_supports_json_follows_the_provider_capability(self) -> None:
        class P:
            supports_json = True

        assert SummaryService(P(), None, None)._supports_json() is True
        assert SummaryService(object(), None, None)._supports_json() is False


class TestParsers:
    def test_labeled_text_with_markdown_and_multiline_values(self, log: _LogRecorder) -> None:
        data = SummaryService(None, None, None)._parse_stream_summary(TEXT_REPLY, json_mode=False)

        assert data["title"] == "Stream Summary - Oct 1, 2026"
        assert data["summary"] == "Chat celebrated a speedrun PB. It stayed friendly all night."
        assert data["key_topics"] == ["speedrun", "giveaways", "raid"]
        assert data["notable_moments"] == ["the PB", "the raid at the end"]
        assert data["sentiment"] == "positive"
        assert log.warnings == []

    def test_text_reply_that_ignores_the_shape_degrades_loudly(self, log: _LogRecorder) -> None:
        data = SummaryService(None, None, None)._parse_stream_summary(
            "  Just a freeform paragraph about the stream.  ", json_mode=False
        )

        assert data["title"] == "Stream Summary"
        assert data["summary"] == "Just a freeform paragraph about the stream."
        assert data["key_topics"] == [] and data["sentiment"] == "neutral"
        assert len(log.warnings) == 1 and "labeled-text" in log.warnings[0]

    def test_weekly_text(self, log: _LogRecorder) -> None:
        data = SummaryService(None, None, None)._parse_weekly_summary(
            "TITLE: Weekly Summary - Week of Oct 1\nSUMMARY: A busy week.", json_mode=False
        )
        assert data == {"title": "Weekly Summary - Week of Oct 1", "summary": "A busy week."}

    def test_json_reply(self, log: _LogRecorder) -> None:
        reply = json.dumps({"title": "T", "summary": "S", "key_topics": ["a"], "sentiment": "neutral"})
        data = SummaryService(None, None, None)._parse_stream_summary(reply, json_mode=True)
        assert data["title"] == "T" and data["summary"] == "S" and data["key_topics"] == ["a"]
        assert data["notable_moments"] == []  # defaulted
        assert log.warnings == []

    @pytest.mark.parametrize("reply", ["[1, 2]", '"just a string"', "{}", '{"summary": ""}'])
    def test_json_reply_of_the_wrong_shape_degrades_loudly(
        self, log: _LogRecorder, reply: str
    ) -> None:
        data = SummaryService(None, None, None)._parse_weekly_summary(reply, json_mode=True)
        assert data["title"] == "Weekly Summary"
        assert data["summary"] == reply.strip()
        assert len(log.warnings) == 1 and "JSON" in log.warnings[0]

    def test_json_mode_flag_with_undecodable_text_degrades_loudly(self, log: _LogRecorder) -> None:
        data = SummaryService(None, None, None)._parse_weekly_summary("not json", json_mode=True)
        assert data["summary"] == "not json"
        assert len(log.warnings) == 1


class TestFlowsThroughTheRealProvider:
    async def test_stream_summary_text_only_model(
        self, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
    ) -> None:
        wire: list[dict[str, Any]] = []
        service, saved = _service(_provider(TEXT_REPLY, wire), monkeypatch)

        result = await service.generate_stream_summary(1, STREAM_START, STREAM_END)

        assert "format" not in wire[0]  # text-only model: no structured-output params
        assert "JSON" not in wire[0]["prompt"]
        assert "TITLE:" in wire[0]["prompt"]
        assert result["insight_id"] == 77
        assert result["title"] == "Stream Summary - Oct 1, 2026"
        assert result["key_topics"] == ["speedrun", "giveaways", "raid"]
        assert result["viewer_stats"]["unique_chatters"] == 2
        assert saved[0]["insight_type"] == "stream_summary"
        assert saved[0]["content"].startswith("Chat celebrated")
        assert saved[0]["metadata"]["sentiment"] == "positive"

    async def test_stream_summary_json_capable_model(
        self, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
    ) -> None:
        wire: list[dict[str, Any]] = []
        reply = json.dumps(
            {"title": "Big Night", "summary": "Fun.", "key_topics": ["pb"], "sentiment": "positive"}
        )
        service, saved = _service(_provider(reply, wire, OLLAMA_SUPPORTS_JSON=True), monkeypatch)

        result = await service.generate_stream_summary(1, STREAM_START, STREAM_END)

        assert wire[0]["format"] == "json"
        assert "summary in JSON format" in wire[0]["prompt"]
        assert result["title"] == "Big Night" and result["key_topics"] == ["pb"]
        assert saved[0]["title"] == "Big Night"

    async def test_weekly_summary_both_modes(
        self, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
    ) -> None:
        wire: list[dict[str, Any]] = []
        text_service, _ = _service(
            _provider("TITLE: Week\nSUMMARY: Busy.", wire), monkeypatch
        )
        text_result = await text_service.generate_weekly_summary(1)
        assert "format" not in wire[0] and text_result["summary"] == "Busy."
        assert text_result["stream_count"] == 1 and text_result["insight_id"] == 77

        json_wire: list[dict[str, Any]] = []
        json_service, _ = _service(
            _provider('{"title": "W", "summary": "J."}', json_wire, OLLAMA_SUPPORTS_JSON=True),
            monkeypatch,
        )
        json_result = await json_service.generate_weekly_summary(1)
        assert json_wire[0]["format"] == "json" and json_result["summary"] == "J."

    async def test_provider_failure_propagates_loudly(
        self, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
    ) -> None:
        provider = AIProviderService(FakeConfig())  # type: ignore[arg-type]
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "x"}))
        )
        service, saved = _service(provider, monkeypatch)

        with pytest.raises(httpx.HTTPStatusError):
            await service.generate_stream_summary(1, STREAM_START, STREAM_END)

        assert saved == []  # nothing persisted from a failed generation
