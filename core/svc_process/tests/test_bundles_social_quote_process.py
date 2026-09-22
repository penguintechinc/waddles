"""Tests for bundles.social_quote_process: quote command transform.

Tests the !quote command parsing, database lookups for random/ID fetch, and
quote addition intent storage for the action stage.
"""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

from bundles.social_quote_process import transform


def _event(
    text: str,
    *,
    actor: str | None = "penguin",
    platform: str = "twitch",
    **payload_overrides: object,
) -> PlatformEvent:
    """Factory to build a PlatformEvent for testing."""
    payload: dict[str, object] = {"text": text, **payload_overrides}
    return PlatformEvent(
        platform=platform,
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with a minimal `quotes` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE quotes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, quote_text TEXT, "
                "quoted_username TEXT, is_approved BOOLEAN, deleted_at TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


class TestQuoteCommands:
    async def test_bare_quote_command_shows_help(self, dal: AsyncDB) -> None:
        """!quote with no args shows help text."""
        result = await transform(_event("!quote"))
        assert result is not None
        assert "Quote commands:" in result.payload["text"]
        assert "!quote add" in result.payload["text"]
        assert "!quote random" in result.payload["text"]

    async def test_add_quote_stores_intent(self, dal: AsyncDB) -> None:
        """!quote add <text> stores add action in payload for action stage."""
        result = await transform(_event("!quote add hello world this is a quote"))
        assert result is not None
        assert result.payload["_quote_action"] == "add"
        assert result.payload["_quote_text"] == "hello world this is a quote"
        assert result.payload["_actor"] == "penguin"

    async def test_add_quote_without_text_shows_usage(self, dal: AsyncDB) -> None:
        """!quote add with no text shows usage hint."""
        result = await transform(_event("!quote add"))
        assert result is not None
        assert "Usage:" in result.payload["text"]
        assert "!quote add" in result.payload["text"]

    async def test_add_quote_with_only_whitespace_shows_usage(self, dal: AsyncDB) -> None:
        """!quote add with only whitespace shows usage hint."""
        result = await transform(_event("!quote add    "))
        assert result is not None
        assert "Usage:" in result.payload["text"]

    async def test_quote_by_id_makes_db_call(self, dal: AsyncDB) -> None:
        """!quote <id> attempts database lookup."""
        # Seed a quote
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO quotes (id, quote_text, quoted_username, is_approved, deleted_at) "
                    "VALUES (42, 'test quote', 'bob', 1, NULL)"
                )
            )

        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!quote 42"))

        assert result is not None
        assert "#42:" in result.payload["text"]
        assert "test" in result.payload["text"]
        assert "bob" in result.payload["text"]

    async def test_quote_by_id_not_found(self, dal: AsyncDB) -> None:
        """!quote <id> when quote not found shows not-found message."""
        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!quote 999"))

        assert result is not None
        assert "not found" in result.payload["text"]

    async def test_quote_random_makes_db_call(self, dal: AsyncDB) -> None:
        """!quote random attempts database lookup."""
        # Seed an approved quote
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO quotes (quote_text, quoted_username, is_approved, deleted_at) "
                    "VALUES ('random quote', 'alice', 1, NULL)"
                )
            )

        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!quote random"))

        assert result is not None
        assert "#" in result.payload["text"]

    async def test_quote_random_not_found(self, dal: AsyncDB) -> None:
        """!quote random when no quotes exist shows not-found message."""
        # Don't seed any quotes
        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!quote random"))

        assert result is not None
        assert "No quotes found" in result.payload["text"]

    async def test_unknown_quote_subcommand(self, dal: AsyncDB) -> None:
        """!quote <unknown> shows unknown command message."""
        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!quote nonsense"))
        assert result is not None
        assert "Unknown quote command:" in result.payload["text"]

    async def test_quote_commands_case_insensitive(self, dal: AsyncDB) -> None:
        """Quote subcommands are case-insensitive."""
        # Seed an approved quote
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO quotes (quote_text, quoted_username, is_approved, deleted_at) "
                    "VALUES ('random quote', 'alice', 1, NULL)"
                )
            )

        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!QUOTE RANDOM"))

        assert result is not None
        assert "#" in result.payload["text"]

    async def test_quote_id_with_leading_zeros(self, dal: AsyncDB) -> None:
        """!quote 0042 is treated as numeric ID."""
        # Seed a quote with id 42
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO quotes (id, quote_text, quoted_username, is_approved, deleted_at) "
                    "VALUES (42, 'test quote', 'bob', 1, NULL)"
                )
            )

        with bundle_context(tenant="acme", community="1", app_id="waddles.social.quote.default"):
            result = await transform(_event("!quote 0042"))

        assert result is not None
        assert "#42:" in result.payload["text"]


class TestNonQuoteChatter:
    async def test_regular_message_is_no_reply(self, dal: AsyncDB) -> None:
        """Regular chatter (no !quote prefix) returns None."""
        assert await transform(_event("hello everyone")) is None
        assert await transform(_event("just chatting")) is None

    async def test_empty_message_is_no_reply(self, dal: AsyncDB) -> None:
        """Empty or whitespace-only messages return None."""
        assert await transform(_event("")) is None
        assert await transform(_event("   ")) is None

    async def test_missing_text_field_raises(self, dal: AsyncDB) -> None:
        """Event with missing 'text' field raises ValueError."""
        event = PlatformEvent(
            platform="twitch",
            event_type="message",
            actor="penguin",
            payload={},  # missing text
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)

    async def test_non_string_text_raises(self, dal: AsyncDB) -> None:
        """Event with non-string text field raises ValueError."""
        event = PlatformEvent(
            platform="twitch",
            event_type="message",
            actor="penguin",
            payload={"text": 123},  # not a string
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)


class TestEdgeCases:
    async def test_payload_fields_preserved_on_reply(self, dal: AsyncDB) -> None:
        """Payload fields like channel_id are preserved on reply."""
        result = await transform(_event("!quote", channel_id="chan-42"))
        assert result is not None
        assert result.payload["channel_id"] == "chan-42"

    async def test_top_level_fields_preserved(self, dal: AsyncDB) -> None:
        """Top-level PlatformEvent fields are never mutated."""
        event = _event("!quote", platform="discord")
        result = await transform(event)
        assert result is not None
        assert result.platform == "discord"
        assert result.event_type == "message"
        assert result.actor == "penguin"

    async def test_original_event_not_mutated(self, dal: AsyncDB) -> None:
        """Transform returns a new event, doesn't mutate the input."""
        event = _event("!quote add test")
        result = await transform(event)
        assert event.payload["text"] == "!quote add test"
        assert result is not event
        assert "_quote_action" in result.payload

    async def test_missing_actor_handled_gracefully(self, dal: AsyncDB) -> None:
        """Quote add with no actor stores None gracefully."""
        result = await transform(_event("!quote add test", actor=None))
        assert result is not None
        assert result.payload["_quote_action"] == "add"
        assert result.payload["_actor"] is None

    async def test_db_exception_on_random_quote(self, dal: AsyncDB) -> None:
        """Database exception on random quote fetch returns None gracefully."""
        # Close the connection pool to force a DB error on query attempt
        await dal.close()
        try:
            with bundle_context(
                tenant="acme", community="1", app_id="waddles.social.quote.default"
            ):
                result = await transform(_event("!quote random"))

            assert result is not None
            assert "No quotes found" in result.payload["text"]
        finally:
            reset_bundle_dal_for_tests()

    async def test_db_exception_on_id_quote(self, dal: AsyncDB) -> None:
        """Database exception on ID quote fetch returns None gracefully."""
        # Close the connection pool to force a DB error on query attempt
        await dal.close()
        try:
            with bundle_context(
                tenant="acme", community="1", app_id="waddles.social.quote.default"
            ):
                result = await transform(_event("!quote 42"))

            assert result is not None
            assert "not found" in result.payload["text"]
        finally:
            reset_bundle_dal_for_tests()

    async def test_quote_with_extra_spaces(self, dal: AsyncDB) -> None:
        """Extra spaces in command are handled correctly."""
        result = await transform(_event("!quote    add    hello world"))
        assert result is not None
        assert result.payload["_quote_action"] == "add"
        assert result.payload["_quote_text"] == "hello world"

    async def test_very_long_quote_text(self, dal: AsyncDB) -> None:
        """Very long quote text is stored as-is."""
        long_text = "a" * 1000
        result = await transform(_event(f"!quote add {long_text}"))
        assert result is not None
        assert result.payload["_quote_text"] == long_text
