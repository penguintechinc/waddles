"""Tests for `builtin_handlers.discord_ingest.normalize` -- Discord gateway ingest entrypoint."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.discord_ingest import normalize


class TestNormalize:
    async def test_normalizes_valid_raw_discord_event(self) -> None:
        raw = {
            "platform": "discord",
            "guild_id": "7",
            "channel_id": "42",
            "message_id": "123",
            "author_id": "555",
            "author_username": "alice",
            "content": "  hello waddlebot  ",
        }
        result = await normalize(raw)
        assert isinstance(result, PlatformEvent)
        assert result.platform == "discord"
        assert result.event_type == "message"
        assert result.actor == "alice"
        assert result.payload == {
            "text": "hello waddlebot",
            "guild_id": "7",
            "channel_id": "42",
            "message_id": "123",
            "author_id": "555",
        }
        assert result.occurred_at

    async def test_falls_back_to_author_id_when_username_missing(self) -> None:
        result = await normalize({"content": "hi", "author_id": "555"})
        assert result.actor == "555"

    async def test_preserves_explicit_timestamp(self) -> None:
        result = await normalize(
            {"content": "hi", "author_id": "555", "occurred_at": "2026-01-01T00:00:00+00:00"}
        )
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"

    async def test_missing_content_raises(self) -> None:
        with pytest.raises(ValueError, match="content"):
            await normalize({"author_id": "555"})

    async def test_empty_content_raises(self) -> None:
        with pytest.raises(ValueError, match="content"):
            await normalize({"content": "", "author_id": "555"})

    async def test_missing_author_id_raises(self) -> None:
        with pytest.raises(ValueError, match="author_id"):
            await normalize({"content": "hi"})
