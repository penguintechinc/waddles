"""Tests for `builtin_handlers.twitch_ingest.normalize` -- Twitch chat event -> `PlatformEvent`."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.twitch_ingest import normalize


async def test_normalizes_a_real_chat_message() -> None:
    raw = {
        "platform": "twitch",
        "channel_name": "waddlebot",
        "author_username": "alice",
        "content": "  hello chat  ",
    }
    event = await normalize(raw)

    assert isinstance(event, PlatformEvent)
    assert event.platform == "twitch"
    assert event.event_type == "message"
    assert event.actor == "alice"
    assert event.payload["text"] == "hello chat"
    assert event.payload["channel_name"] == "waddlebot"
    assert event.payload["author"] == "alice"
    assert event.occurred_at


async def test_falls_back_to_unknown_when_sender_missing() -> None:
    raw = {"channel_name": "waddlebot", "content": "hi"}
    event = await normalize(raw)
    assert event.actor == "unknown"
    assert event.payload["author"] == "unknown"


async def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        await normalize({"channel_name": "waddlebot"})


async def test_missing_channel_name_raises() -> None:
    with pytest.raises(ValueError, match="channel_name"):
        await normalize({"content": "hi"})


async def test_preserves_supplied_occurred_at() -> None:
    raw = {
        "channel_name": "waddlebot",
        "content": "hi",
        "occurred_at": "2026-01-01T00:00:00+00:00",
    }
    event = await normalize(raw)
    assert event.occurred_at == "2026-01-01T00:00:00+00:00"


async def test_missing_ircv3_fields_default_absent_never_raise() -> None:
    """No `author_id`/`badges`/etc. on the raw event -- all default, no `ValueError`."""
    raw = {"channel_name": "waddlebot", "content": "hi", "author_username": "alice"}
    event = await normalize(raw)

    assert event.payload["author_id"] is None
    assert event.payload["user_id"] is None
    assert event.payload["display_name"] is None
    assert event.payload["message_id"] is None
    assert event.payload["room_id"] is None
    assert event.payload["badges"] == []
    assert event.payload["is_mod"] is False
    assert event.payload["is_subscriber"] is False
    assert event.payload["is_vip"] is False
    assert event.payload["is_broadcaster"] is False


async def test_ircv3_fields_pass_through_unchanged() -> None:
    """Real values set by `receivers/twitch_irc.py` ride straight onto `payload` unmodified."""
    raw = {
        "channel_name": "waddlebot",
        "content": "hi mods",
        "author_username": "penguinfan",
        "author_id": "87654321",
        "user_id": "87654321",
        "display_name": "PenguinFan",
        "message_id": "msg-abc-123",
        "room_id": "555444",
        "badges": ["moderator", "subscriber", "vip"],
        "is_mod": True,
        "is_subscriber": True,
        "is_vip": True,
        "is_broadcaster": False,
    }
    event = await normalize(raw)

    assert event.actor == "penguinfan"  # actor value unchanged by the new fields
    assert event.payload["author_id"] == "87654321"
    assert event.payload["user_id"] == "87654321"
    assert event.payload["display_name"] == "PenguinFan"
    assert event.payload["message_id"] == "msg-abc-123"
    assert event.payload["room_id"] == "555444"
    assert event.payload["badges"] == ["moderator", "subscriber", "vip"]
    assert event.payload["is_mod"] is True
    assert event.payload["is_subscriber"] is True
    assert event.payload["is_vip"] is True
    assert event.payload["is_broadcaster"] is False


async def test_non_list_badges_on_raw_event_defaults_to_empty_list() -> None:
    """Defensive type guard -- a malformed non-list `badges` never propagates, never crashes."""
    raw = {"channel_name": "waddlebot", "content": "hi", "badges": "not-a-list"}
    event = await normalize(raw)
    assert event.payload["badges"] == []
