"""Tests for `builtin_handlers.twitch_eventsub_ingest.normalize`.

Raw EventSub notification -> `PlatformEvent`.
"""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.twitch_eventsub_ingest import normalize


async def test_normalizes_a_follow_event() -> None:
    raw = {
        "platform": "twitch",
        "event_type": "channel.follow",
        "broadcaster_id": "999",
        "broadcaster_login": "waddlebot",
        "user_id": "555",
        "user_login": "alice",
        "user_display_name": "Alice",
        "metadata": {},
    }
    event = await normalize(raw)

    assert isinstance(event, PlatformEvent)
    assert event.platform == "twitch"
    assert event.event_type == "channel.follow"
    assert event.actor == "alice"
    assert event.payload["broadcaster_id"] == "999"
    assert event.occurred_at


async def test_normalizes_a_cheer_event_with_metadata() -> None:
    raw = {
        "event_type": "channel.cheer",
        "broadcaster_id": "999",
        "user_login": "bob",
        "metadata": {"bits": 500},
    }
    event = await normalize(raw)
    assert event.payload["metadata"] == {"bits": 500}


async def test_unsupported_event_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported event_type"):
        await normalize({"event_type": "channel.update", "broadcaster_id": "999"})


async def test_missing_broadcaster_id_raises() -> None:
    with pytest.raises(ValueError, match="broadcaster_id"):
        await normalize({"event_type": "channel.follow"})


async def test_falls_back_to_broadcaster_id_when_no_user_identity() -> None:
    raw = {"event_type": "channel.raid", "broadcaster_id": "999", "metadata": {"viewers": 10}}
    event = await normalize(raw)
    assert event.actor == "999"


async def test_normalizes_a_stream_online_event() -> None:
    """Gh #287 S10 -- `stream.online` carries no `user_*` identity, only broadcaster fields."""
    raw = {
        "platform": "twitch",
        "event_type": "stream.online",
        "broadcaster_id": "999",
        "broadcaster_login": "waddlebot",
        "metadata": {"type": "live", "started_at": "2026-09-11T12:00:00Z"},
    }
    event = await normalize(raw)

    assert event.event_type == "stream.online"
    assert event.actor == "999"  # falls back to broadcaster_id, no user_* fields present
    assert event.payload["broadcaster_id"] == "999"
    assert event.payload["broadcaster_login"] == "waddlebot"
    assert event.payload["metadata"] == {"type": "live", "started_at": "2026-09-11T12:00:00Z"}


async def test_normalizes_a_stream_offline_event() -> None:
    raw = {
        "platform": "twitch",
        "event_type": "stream.offline",
        "broadcaster_id": "999",
        "broadcaster_login": "waddlebot",
    }
    event = await normalize(raw)

    assert event.event_type == "stream.offline"
    assert event.payload["broadcaster_id"] == "999"
    assert event.payload["metadata"] == {}
