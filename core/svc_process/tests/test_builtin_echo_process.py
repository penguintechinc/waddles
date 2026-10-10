"""Tests for `builtin_handlers.echo_process.transform` -- the demo process entrypoint."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.echo_process import transform


def _event(**payload_overrides: object) -> PlatformEvent:
    payload: dict[str, object] = {"text": "hello world", **payload_overrides}
    return PlatformEvent(
        platform="twitch",
        event_type="message",
        actor="penguin",
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestTransform:
    async def test_transforms_valid_event(self) -> None:
        event = _event()
        result = await transform(event)
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "HELLO WORLD"
        assert result.payload["word_count"] == 2
        assert result.payload["processed"] is True
        assert result.platform == "twitch"  # top-level fields preserved
        assert result.event_type == "message"
        assert result.actor == "penguin"
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"

    async def test_preserves_channel_id_and_guild_id(self) -> None:
        """Crucial regression: routing IDs must survive the transform untouched."""
        event = _event(channel_id="chan-42", guild_id="guild-7")
        result = await transform(event)
        assert result.payload["channel_id"] == "chan-42"
        assert result.payload["guild_id"] == "guild-7"

    async def test_missing_text_raises(self) -> None:
        event = PlatformEvent(
            platform="twitch",
            event_type="message",
            actor=None,
            payload={},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)

    async def test_preserves_other_payload_fields(self) -> None:
        event = _event(extra="keep-me")
        result = await transform(event)
        assert result.payload["extra"] == "keep-me"
        assert result.payload["word_count"] == 2

    async def test_original_event_is_not_mutated(self) -> None:
        """`PlatformEvent` is frozen -- `transform` must return a new instance."""
        event = _event()
        result = await transform(event)
        assert event.payload["text"] == "hello world"
        assert result is not event
