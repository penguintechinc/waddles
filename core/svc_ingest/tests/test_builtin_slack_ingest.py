"""Tests for `builtin_handlers.slack_ingest.normalize` -- Slack Socket Mode ingest entrypoint."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.slack_ingest import normalize


class TestNormalizeMessage:
    async def test_normalizes_valid_raw_message_event(self) -> None:
        raw = {
            "platform": "slack",
            "event_type": "message",
            "text": "  hello waddlebot  ",
            "channel_id": "C123",
            "team_id": "T456",
            "thread_ts": "1700000000.000100",
            "message_ts": "1700000001.000200",
            "platform_user_id": "U789",
            "display_name": None,
        }
        result = await normalize(raw)
        assert isinstance(result, PlatformEvent)
        assert result.platform == "slack"
        assert result.event_type == "message"
        assert result.actor == "U789"
        assert result.payload == {
            "text": "hello waddlebot",
            "channel_id": "C123",
            "team_id": "T456",
            "thread_ts": "1700000000.000100",
            "message_ts": "1700000001.000200",
            "platform_user_id": "U789",
            "display_name": None,
        }
        assert result.occurred_at

    async def test_preserves_explicit_timestamp(self) -> None:
        result = await normalize(
            {
                "event_type": "message",
                "text": "hi",
                "platform_user_id": "U1",
                "occurred_at": "2026-01-01T00:00:00+00:00",
            }
        )
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"

    async def test_missing_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"event_type": "message", "platform_user_id": "U1"})

    async def test_empty_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"event_type": "message", "text": "", "platform_user_id": "U1"})


class TestNormalizeAppMention:
    async def test_requires_text_same_as_message(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"event_type": "app_mention", "platform_user_id": "U1"})

    async def test_normalizes_valid_app_mention(self) -> None:
        result = await normalize(
            {"event_type": "app_mention", "text": "hey @bot", "platform_user_id": "U1"}
        )
        assert result.event_type == "app_mention"
        assert result.payload["text"] == "hey @bot"


class TestNormalizeMemberJoinedChannel:
    async def test_does_not_require_text(self) -> None:
        result = await normalize(
            {
                "event_type": "member_joined_channel",
                "channel_id": "C1",
                "platform_user_id": "U1",
            }
        )
        assert result.event_type == "member_joined_channel"
        assert result.payload["text"] is None

    async def test_actor_is_the_joining_user(self) -> None:
        result = await normalize(
            {"event_type": "member_joined_channel", "platform_user_id": "U999"}
        )
        assert result.actor == "U999"


class TestNormalizeValidation:
    async def test_missing_event_type_raises(self) -> None:
        with pytest.raises(ValueError, match="event_type"):
            await normalize({"platform_user_id": "U1", "text": "hi"})

    async def test_empty_event_type_raises(self) -> None:
        with pytest.raises(ValueError, match="event_type"):
            await normalize({"event_type": "", "platform_user_id": "U1", "text": "hi"})

    async def test_missing_platform_user_id_raises(self) -> None:
        with pytest.raises(ValueError, match="platform_user_id"):
            await normalize({"event_type": "message", "text": "hi"})

    async def test_empty_platform_user_id_raises(self) -> None:
        with pytest.raises(ValueError, match="platform_user_id"):
            await normalize({"event_type": "message", "text": "hi", "platform_user_id": ""})
