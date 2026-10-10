"""Tests for `builtin_handlers.marketing_engagement_process.transform`."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.marketing_engagement_process import transform


def _event(
    event_type: str = "engagement",
    text: str = "poll created",
    payload_override: dict[str, object] | None = None,
) -> PlatformEvent:
    """Create a test PlatformEvent for engagement."""
    payload = (
        payload_override if payload_override is not None else {"text": text, "channel_id": "chan-1"}
    )
    return PlatformEvent(
        platform="discord",
        event_type=event_type,
        actor="penguin",
        payload=payload,
        occurred_at="2026-09-04T00:00:00+00:00",
    )


class TestTransform:
    """Tests for engagement event validation and passthrough."""

    async def test_engagement_event_passes_through(self) -> None:
        """Engagement events are validated and passed through unchanged."""
        result = await transform(_event())
        assert isinstance(result, PlatformEvent)
        assert result.event_type == "engagement"
        assert result.payload["text"] == "poll created"
        assert result.payload["channel_id"] == "chan-1"

    async def test_poll_create_event_passes_through(self) -> None:
        """poll_create events pass through validation."""
        result = await transform(_event(event_type="poll_create"))
        assert isinstance(result, PlatformEvent)
        assert result.event_type == "poll_create"

    async def test_poll_vote_event_passes_through(self) -> None:
        """poll_vote events pass through validation."""
        result = await transform(_event(event_type="poll_vote"))
        assert isinstance(result, PlatformEvent)
        assert result.event_type == "poll_vote"

    async def test_form_submit_event_passes_through(self) -> None:
        """form_submit events pass through validation."""
        result = await transform(_event(event_type="form_submit"))
        assert isinstance(result, PlatformEvent)
        assert result.event_type == "form_submit"

    async def test_non_engagement_event_returns_none(self) -> None:
        """Non-engagement event types return None (no reply)."""
        result = await transform(_event(event_type="message"))
        assert result is None

    async def test_ordinary_chatter_returns_none(self) -> None:
        """Ordinary chat messages return None."""
        result = await transform(_event(event_type="random_chatter"))
        assert result is None

    async def test_event_with_no_text_still_passes(self) -> None:
        """Events without text field are still valid if other fields present."""
        result = await transform(_event(payload_override={"channel_id": "chan-1"}))
        assert isinstance(result, PlatformEvent)

    async def test_event_with_dict_text_raises(self) -> None:
        """Malformed 'text' field (dict instead of string) raises ValueError."""
        with pytest.raises(ValueError, match="text.*string"):
            await transform(_event(payload_override={"text": {"nested": "dict"}}))

    async def test_event_with_empty_payload_raises(self) -> None:
        """Empty payload raises ValueError."""
        with pytest.raises(ValueError, match="payload"):
            await transform(_event(payload_override={}))

    async def test_preserves_all_payload_fields(self) -> None:
        """All payload fields are preserved through the transform."""
        payload = {
            "text": "poll created",
            "channel_id": "chan-1",
            "author_id": "user-1",
            "poll_id": "poll-123",
            "options": ["yes", "no"],
        }
        result = await transform(_event(payload_override=payload))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "poll created"
        assert result.payload["channel_id"] == "chan-1"
        assert result.payload["author_id"] == "user-1"
        assert result.payload["poll_id"] == "poll-123"
        assert result.payload["options"] == ["yes", "no"]
