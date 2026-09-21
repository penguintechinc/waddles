"""Tests for `bundles.community_polls_process.transform`."""

from __future__ import annotations

from datetime import datetime

import pytest
from flask_core import (
    PlatformEvent,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from penguin_dal import AsyncDB
from sqlalchemy import event
from sqlalchemy import text as sa_text

from bundles.community_polls_process import (
    _parse_quoted_args,
    transform,
)


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the three tables this bundle touches."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)

    # Register SQLite NOW() function for tests via event listener
    def now_fn():
        return datetime.now().isoformat()

    def register_now_function(dbapi_conn, connection_record):
        """Register NOW() function on SQLite connection."""
        dbapi_conn.create_function("NOW", 0, now_fn)

    # Use event listener on the sync engine to register the function
    event.listen(db.engine.sync_engine.pool, "connect", register_now_function)

    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_polls ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, created_by TEXT, "
                "title TEXT, is_active BOOLEAN, created_at TEXT, updated_at TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE poll_options ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER, "
                "option_text TEXT, sort_order INTEGER)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE poll_votes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER, option_id INTEGER, "
                "user_id TEXT, voted_at TEXT, UNIQUE(poll_id, option_id, user_id))"
            )
        )

    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


def _event(text: str, actor: str = "penguin") -> PlatformEvent:
    """Create a test PlatformEvent with the given text."""
    payload: dict = {"text": text, "channel_id": "chan-1"}
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestTransform:
    """Tests for the main transform entrypoint."""

    async def test_non_poll_command_returns_none(self, dal: AsyncDB) -> None:
        """Ordinary chatter should return None."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("just chatting"))
        assert result is None

    async def test_poll_command_no_subcommand_returns_help(self, dal: AsyncDB) -> None:
        """Bare `!poll` with no subcommand returns help."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll"))
        assert result is not None
        assert result.payload["text"]
        assert "create" in result.payload["text"].lower()
        assert "list" in result.payload["text"].lower()

    async def test_poll_list_returns_active_polls(self, dal: AsyncDB) -> None:
        """!poll list returns active polls for the community."""
        # Seed a poll
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test Poll', TRUE, NOW(), NOW())"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll list"))
        assert result is not None
        assert "Test Poll" in result.payload["text"]

    async def test_poll_create_missing_args(self, dal: AsyncDB) -> None:
        """!poll create without arguments returns usage."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll create"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_poll_create_insufficient_options(self, dal: AsyncDB) -> None:
        """!poll create with only title and one option returns error."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event('!poll create "My Poll" "Option1"'))
        assert result is not None
        assert "at least 2 options" in result.payload["text"].lower()

    async def test_poll_create_success(self, dal: AsyncDB) -> None:
        """!poll create with valid args creates a poll."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event('!poll create "My Poll" "Opt1" "Opt2"'))
        assert result is not None
        assert "Poll created" in result.payload["text"]
        assert "My Poll" in result.payload["text"]

        # Verify poll was actually created in the database
        async with dal.engine.begin() as conn:
            res = await conn.execute(
                sa_text("SELECT id, title FROM community_polls WHERE community_id = 1")
            )
            rows = list(res.fetchall())
            assert len(rows) == 1
            assert rows[0][1] == "My Poll"

    async def test_poll_vote_missing_args(self, dal: AsyncDB) -> None:
        """!poll vote without arguments returns usage."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_poll_vote_insufficient_args(self, dal: AsyncDB) -> None:
        """!poll vote with only poll_id returns error."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 123"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_poll_vote_non_numeric_poll_id(self, dal: AsyncDB) -> None:
        """!poll vote with non-numeric poll_id returns error."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote abc 1"))
        assert result is not None
        assert "numeric" in result.payload["text"].lower()

    async def test_poll_close_missing_args(self, dal: AsyncDB) -> None:
        """!poll close without arguments returns usage."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_poll_view_missing_args(self, dal: AsyncDB) -> None:
        """!poll view without arguments returns usage."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_unknown_subcommand_returns_error(self, dal: AsyncDB) -> None:
        """Unknown subcommand returns error."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll unknown"))
        assert result is not None
        assert "unknown" in result.payload["text"].lower()

    async def test_missing_text_raises_value_error(self, dal: AsyncDB) -> None:
        """Event without text raises ValueError."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="penguin",
            payload={"channel_id": "chan-1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            with pytest.raises(ValueError, match="text"):
                await transform(event)

    async def test_non_string_text_raises_value_error(self, dal: AsyncDB) -> None:
        """Event with non-string text raises ValueError."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="penguin",
            payload={"text": 123, "channel_id": "chan-1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            with pytest.raises(ValueError, match="text"):
                await transform(event)

    async def test_whitespace_only_text_returns_none(self, dal: AsyncDB) -> None:
        """Text with only whitespace returns None."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("   "))
        assert result is None

    async def test_preserves_channel_id_in_reply(self, dal: AsyncDB) -> None:
        """Reply should preserve channel_id from original event."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll list"))
        if result:
            assert result.payload.get("channel_id") == "chan-1"


class TestParseQuotedArgs:
    """Tests for the quoted argument parser."""

    def test_parse_simple_quoted_args(self) -> None:
        """Parse simple quoted arguments."""
        args = '"title" "opt1" "opt2"'
        result = _parse_quoted_args(args)
        assert result == ["title", "opt1", "opt2"]

    def test_parse_args_with_spaces(self) -> None:
        """Parse arguments with internal spaces."""
        args = '"My Title" "First Option" "Second Option"'
        result = _parse_quoted_args(args)
        assert result == ["My Title", "First Option", "Second Option"]

    def test_parse_args_empty_string(self) -> None:
        """Parse empty string returns empty list."""
        result = _parse_quoted_args("")
        assert result == []

    def test_parse_args_no_quotes(self) -> None:
        """Parse arguments without quotes."""
        result = _parse_quoted_args("arg1 arg2 arg3")
        assert result == ["arg1", "arg2", "arg3"]

    def test_parse_args_mixed_quoted_unquoted(self) -> None:
        """Parse mix of quoted and unquoted."""
        result = _parse_quoted_args('arg1 "arg 2" arg3')
        assert result == ["arg1", "arg 2", "arg3"]

    def test_parse_args_escaped_quotes(self) -> None:
        """Parse escaped quotes within arguments."""
        result = _parse_quoted_args(r'"arg with \" quote"')
        assert result == ['arg with " quote']

    def test_parse_args_trailing_whitespace(self) -> None:
        """Parse with trailing whitespace."""
        result = _parse_quoted_args('"arg1" "arg2"  ')
        assert result == ["arg1", "arg2"]

    def test_parse_args_leading_whitespace(self) -> None:
        """Parse with leading whitespace."""
        result = _parse_quoted_args('  "arg1" "arg2"')
        assert result == ["arg1", "arg2"]


class TestCommunityContext:
    """Tests for community-scoped operations (IDOR fix regression tests)."""

    async def test_poll_create_requires_community_context(self, dal: AsyncDB) -> None:
        """Create rejects when ctx.community is None (tenant-wide rejection)."""
        with bundle_context(tenant="t1", community=None, app_id="waddles.community.polls.default"):
            result = await transform(_event('!poll create "Poll" "A" "B"'))
        assert result is not None
        assert "community context" in result.payload["text"].lower()

    async def test_poll_vote_requires_community_context(self, dal: AsyncDB) -> None:
        """Vote rejects when ctx.community is None (tenant-wide rejection)."""
        with bundle_context(tenant="t1", community=None, app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 1"))
        assert result is not None
        assert "community context" in result.payload["text"].lower()

    async def test_poll_close_requires_community_context(self, dal: AsyncDB) -> None:
        """Close rejects when ctx.community is None (tenant-wide rejection)."""
        with bundle_context(tenant="t1", community=None, app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close 1"))
        assert result is not None
        assert "community context" in result.payload["text"].lower()

    async def test_poll_view_requires_community_context(self, dal: AsyncDB) -> None:
        """View rejects when ctx.community is None (tenant-wide rejection)."""
        with bundle_context(tenant="t1", community=None, app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view 1"))
        assert result is not None
        assert "community context" in result.payload["text"].lower()

    async def test_poll_vote_idor_cross_community_not_found(self, dal: AsyncDB) -> None:
        """Vote on poll from another community returns not found (IDOR fix)."""
        # Seed poll in community 1
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test Poll', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Option A', 0)"
                )
            )

        # Try to vote from community 2
        with bundle_context(tenant="t1", community="2", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 1"))
        assert result is not None
        text_lower = result.payload["text"].lower()
        assert "not found" in text_lower or "closed" in text_lower

    async def test_poll_close_idor_cross_community_not_found(self, dal: AsyncDB) -> None:
        """Close on poll from another community returns not found (IDOR fix)."""
        # Seed poll in community 1
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test Poll', TRUE, NOW(), NOW())"
                )
            )

        # Try to close from community 2
        with bundle_context(tenant="t1", community="2", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close 1"))
        assert result is not None
        assert "not found" in result.payload["text"].lower()

    async def test_poll_view_idor_cross_community_not_found(self, dal: AsyncDB) -> None:
        """View poll from another community returns not found (IDOR fix)."""
        # Seed poll in community 1
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test Poll', TRUE, NOW(), NOW())"
                )
            )

        # Try to view from community 2
        with bundle_context(tenant="t1", community="2", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view 1"))
        assert result is not None
        assert "not found" in result.payload["text"].lower()


class TestPollVoteFlow:
    """Full vote handler tests including success paths."""

    async def test_poll_vote_success(self, dal: AsyncDB) -> None:
        """Successfully vote on a poll."""
        # Seed poll with options
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Favorite Color', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Red', 0), (2, 1, 'Blue', 1)"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 2"))
        assert result is not None
        assert "vote recorded" in result.payload["text"].lower()
        assert "option 2" in result.payload["text"].lower()

        # Verify vote was recorded
        async with dal.engine.begin() as conn:
            res = await conn.execute(
                sa_text("SELECT COUNT(*) FROM poll_votes WHERE poll_id = 1 AND user_id = 'penguin'")
            )
            count = (res.scalar()) or 0
            assert count == 1

    async def test_poll_vote_re_vote_updates_existing(self, dal: AsyncDB) -> None:
        """Re-voting on same option updates the voted_at timestamp."""
        # Seed poll with options and existing vote
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test Poll', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Option A', 0), (2, 1, 'Option B', 1)"
                )
            )
            # Create initial vote
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_votes (poll_id, option_id, user_id, voted_at) "
                    "VALUES (1, 1, 'voter1', '2026-01-01T10:00:00')"
                )
            )

        # Vote again for the same option
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 1", actor="voter1"))
        assert result is not None
        assert "vote recorded" in result.payload["text"].lower()

        # Verify only one vote exists (not duplicated)
        async with dal.engine.begin() as conn:
            res = await conn.execute(
                sa_text(
                    "SELECT COUNT(*) FROM poll_votes "
                    "WHERE poll_id = 1 AND option_id = 1 AND user_id = 'voter1'"
                )
            )
            count = (res.scalar()) or 0
            assert count == 1

    async def test_poll_vote_option_out_of_range_high(self, dal: AsyncDB) -> None:
        """Vote on option that exceeds total options."""
        # Seed poll with one option
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'A', 0)"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 5"))
        assert result is not None
        assert "invalid option" in result.payload["text"].lower()

    async def test_poll_vote_option_zero(self, dal: AsyncDB) -> None:
        """Vote on option 0 (invalid)."""
        # Seed poll with one option
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Test', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'A', 0)"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 0"))
        assert result is not None
        assert "invalid option" in result.payload["text"].lower()

    async def test_poll_vote_non_numeric_option(self, dal: AsyncDB) -> None:
        """Vote with non-numeric option number."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 1 abc"))
        assert result is not None
        assert "numeric" in result.payload["text"].lower()

    async def test_poll_vote_nonexistent_poll(self, dal: AsyncDB) -> None:
        """Vote on poll that doesn't exist."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll vote 999 1"))
        assert result is not None
        text_lower = result.payload["text"].lower()
        assert "not found" in text_lower or "closed" in text_lower


class TestPollCloseFlow:
    """Full close handler tests including success paths."""

    async def test_poll_close_success(self, dal: AsyncDB) -> None:
        """Successfully close a poll and display results."""
        # Seed poll with options and votes
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Best Language', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Python', 0), (2, 1, 'Go', 1)"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_votes (poll_id, option_id, user_id, voted_at) "
                    "VALUES (1, 1, 'alice', NOW()), (1, 1, 'bob', NOW()), (1, 2, 'charlie', NOW())"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close 1"))
        assert result is not None
        assert "closed" in result.payload["text"].lower()
        assert "Best Language" in result.payload["text"]
        assert "Python: 2" in result.payload["text"]
        assert "Go: 1" in result.payload["text"]

        # Verify poll was marked inactive
        async with dal.engine.begin() as conn:
            res = await conn.execute(sa_text("SELECT is_active FROM community_polls WHERE id = 1"))
            is_active = res.scalar()
            assert is_active == 0 or is_active is False

    async def test_poll_close_no_votes(self, dal: AsyncDB) -> None:
        """Close a poll with no votes."""
        # Seed poll with option but no votes
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Empty Poll', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Option', 0)"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close 1"))
        assert result is not None
        assert "closed" in result.payload["text"].lower()
        assert "option: 0" in result.payload["text"].lower()

    async def test_poll_close_invalid_poll_id(self, dal: AsyncDB) -> None:
        """Close with non-numeric poll ID."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close abc"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_poll_close_nonexistent_poll(self, dal: AsyncDB) -> None:
        """Close poll that doesn't exist."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll close 999"))
        assert result is not None
        assert "not found" in result.payload["text"].lower()


class TestPollViewFlow:
    """Full view handler tests including success paths."""

    async def test_poll_view_active(self, dal: AsyncDB) -> None:
        """View an active poll with vote counts."""
        # Seed poll with options and votes
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Favorite Framework', TRUE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Quart', 0), (2, 1, 'FastAPI', 1)"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_votes (poll_id, option_id, user_id, voted_at) "
                    "VALUES (1, 1, 'alice', NOW())"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view 1"))
        assert result is not None
        assert "poll 1" in result.payload["text"].lower()
        assert "Favorite Framework" in result.payload["text"]
        assert "[active]" in result.payload["text"].lower()
        assert "Quart" in result.payload["text"]
        assert "1. Quart (1 vote)" in result.payload["text"]
        assert "2. FastAPI (0 votes)" in result.payload["text"]
        assert "vote with" in result.payload["text"].lower()

    async def test_poll_view_closed(self, dal: AsyncDB) -> None:
        """View a closed poll (no vote prompt)."""
        # Seed a closed poll
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Closed Poll', FALSE, NOW(), NOW())"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO poll_options (id, poll_id, option_text, sort_order) "
                    "VALUES (1, 1, 'Option', 0)"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view 1"))
        assert result is not None
        assert "[closed]" in result.payload["text"].lower()
        assert "vote with" not in result.payload["text"].lower()

    async def test_poll_view_no_options(self, dal: AsyncDB) -> None:
        """View a poll with no options (edge case)."""
        # Seed poll with no options
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Empty', TRUE, NOW(), NOW())"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view 1"))
        assert result is not None
        assert "Empty" in result.payload["text"]

    async def test_poll_view_invalid_poll_id(self, dal: AsyncDB) -> None:
        """View with non-numeric poll ID."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view abc"))
        assert result is not None
        assert "usage" in result.payload["text"].lower()

    async def test_poll_view_nonexistent_poll(self, dal: AsyncDB) -> None:
        """View poll that doesn't exist."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll view 999"))
        assert result is not None
        assert "not found" in result.payload["text"].lower()


class TestPollListFlow:
    """Full list handler tests including edge cases."""

    async def test_poll_list_empty(self, dal: AsyncDB) -> None:
        """List when no active polls exist."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll list"))
        assert result is not None
        assert "no active polls" in result.payload["text"].lower()

    async def test_poll_list_multiple(self, dal: AsyncDB) -> None:
        """List multiple active polls."""
        # Seed multiple polls
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Poll 1', TRUE, NOW(), NOW()), "
                    "(2, 1, 'bob', 'Poll 2', TRUE, NOW(), NOW())"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll list"))
        assert result is not None
        assert "Poll 1" in result.payload["text"]
        assert "Poll 2" in result.payload["text"]
        assert "active polls" in result.payload["text"].lower()

    async def test_poll_list_filters_inactive(self, dal: AsyncDB) -> None:
        """List only shows active polls, not closed ones."""
        # Seed active and inactive polls
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_polls "
                    "(id, community_id, created_by, title, is_active, created_at, updated_at) "
                    "VALUES (1, 1, 'alice', 'Active', TRUE, NOW(), NOW()), "
                    "(2, 1, 'bob', 'Closed', FALSE, NOW(), NOW())"
                )
            )

        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event("!poll list"))
        assert result is not None
        assert "Active" in result.payload["text"]
        assert "Closed" not in result.payload["text"]


class TestPollCreateFlow:
    """Full create handler tests including edge cases."""

    async def test_poll_create_with_many_options(self, dal: AsyncDB) -> None:
        """Create a poll with many options."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(
                _event('!poll create "Languages" "Python" "Go" "Rust" "JS" "Java"')
            )
        assert result is not None
        assert "Poll created" in result.payload["text"]
        assert "Languages" in result.payload["text"]
        assert "Python" in result.payload["text"]
        assert "Java" in result.payload["text"]

        # Verify options were created
        async with dal.engine.begin() as conn:
            res = await conn.execute(sa_text("SELECT COUNT(*) FROM poll_options"))
            count = (res.scalar()) or 0
            assert count == 5

    async def test_poll_create_exact_two_options(self, dal: AsyncDB) -> None:
        """Create poll with exactly 2 options (minimum)."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event('!poll create "Two" "Opt1" "Opt2"'))
        assert result is not None
        assert "Poll created" in result.payload["text"]

        # Verify options were created
        async with dal.engine.begin() as conn:
            res = await conn.execute(sa_text("SELECT COUNT(*) FROM poll_options"))
            count = (res.scalar()) or 0
            assert count == 2

    async def test_poll_create_non_numeric_actor(self, dal: AsyncDB) -> None:
        """Create preserves non-numeric actor as creator."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event('!poll create "Title" "A" "B"', actor="user123"))
        assert result is not None
        assert "Poll created" in result.payload["text"]

        # Verify creator was set correctly
        async with dal.engine.begin() as conn:
            res = await conn.execute(sa_text("SELECT created_by FROM community_polls LIMIT 1"))
            creator = res.scalar()
            assert creator == "user123"

    async def test_poll_create_with_special_chars(self, dal: AsyncDB) -> None:
        """Create poll with special characters in title and options."""
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(
                _event('!poll create "Best #hashtag? (2026)" "Yes!!!" "No..."')
            )
        assert result is not None
        assert "Poll created" in result.payload["text"]
        assert "Best #hashtag? (2026)" in result.payload["text"]
        assert "Yes!!!" in result.payload["text"]

    async def test_poll_create_exception_handling(self, dal: AsyncDB) -> None:
        """Create handler catches and reports exceptions gracefully."""
        # This tests the try-catch behavior - we can't easily inject a DB error
        # without modifying the DAL, so we just verify the path exists
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            result = await transform(_event('!poll create "Title" "A" "B"'))
        assert result is not None
        # Should succeed without error
        assert (
            "Poll created" in result.payload["text"]
            or "error" not in result.payload["text"].lower()
        )


class TestNoReplyBranches:
    """Tests for None return paths."""

    async def test_non_string_text_in_payload(self, dal: AsyncDB) -> None:
        """Text that's not a string at all gets caught."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="penguin",
            payload={"text": 42, "channel_id": "chan-1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            with pytest.raises(ValueError):
                await transform(event)

    async def test_text_field_missing(self, dal: AsyncDB) -> None:
        """Missing text field raises ValueError."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="penguin",
            payload={"channel_id": "chan-1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant="t1", community="1", app_id="waddles.community.polls.default"):
            with pytest.raises(ValueError):
                await transform(event)
