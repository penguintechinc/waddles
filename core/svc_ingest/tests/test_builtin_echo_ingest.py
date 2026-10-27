"""Tests for `builtin_handlers.echo_ingest.normalize` -- the demo ingest entrypoint."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.echo_ingest import normalize


class TestNormalize:
    async def test_normalizes_valid_event(self) -> None:
        result = await normalize({"source": "twitch", "text": "  hello world  "})
        assert isinstance(result, PlatformEvent)
        assert result.platform == "twitch"
        assert result.event_type == "message"
        assert result.actor == "unknown"
        assert result.payload == {"text": "hello world"}
        assert result.occurred_at

    async def test_preserves_explicit_event_type_and_actor(self) -> None:
        result = await normalize(
            {"source": "discord", "text": "hi", "event_type": "reaction", "actor": "penguin"}
        )
        assert result.event_type == "reaction"
        assert result.actor == "penguin"

    async def test_preserves_explicit_timestamp(self) -> None:
        result = await normalize(
            {"source": "twitch", "text": "hi", "occurred_at": "2026-01-01T00:00:00+00:00"}
        )
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"

    async def test_missing_source_raises(self) -> None:
        with pytest.raises(ValueError, match="source"):
            await normalize({"text": "hi"})

    async def test_missing_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"source": "twitch"})

    async def test_empty_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"source": "twitch", "text": ""})
