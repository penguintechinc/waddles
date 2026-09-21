"""Tests for `bundles.community_reputation_process` -- `!reputation`/`!rep`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

from bundles.community_reputation_process import REPUTATION_TIERS, _reputation_label, transform


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the three tables this bundle reads."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "display_name TEXT, reputation INTEGER, user_id TEXT)"
            )
        )
        await conn.execute(
            sa_text("CREATE TABLE communities (id INTEGER, display_name TEXT, name TEXT)")
        )
        await conn.execute(
            sa_text("CREATE TABLE reputation_global (hub_user_id TEXT, score INTEGER)")
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


def _event(
    text: str, *, actor: str | None = "penguinzplays", author_id: str | None = None
) -> PlatformEvent:
    payload: dict[str, object] = {"text": text}
    if author_id is not None:
        payload["author_id"] = author_id
    return PlatformEvent(
        platform="twitch",
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


async def _run(dal: AsyncDB, text: str, **event_kwargs: object) -> PlatformEvent | None:
    with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
        return await transform(_event(text, **event_kwargs))  # type: ignore[arg-type]


class TestRouting:
    async def test_non_command_text_returns_none(self, dal: AsyncDB) -> None:
        assert await transform(_event("just chatting")) is None

    async def test_malformed_event_raises_value_error(self, dal: AsyncDB) -> None:
        event = PlatformEvent(
            platform="twitch", event_type="message", actor="p", payload={}, occurred_at="x"
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)

    async def test_bare_bang_returns_none(self, dal: AsyncDB) -> None:
        assert await transform(_event("!")) is None


class TestLookup:
    async def test_reply_shows_both_global_and_community_with_labels(self, dal: AsyncDB) -> None:
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_members "
                    "(community_id, platform, platform_user_id, display_name, reputation, user_id) "
                    "VALUES (4, 'twitch', 'u-123', 'penguinzplays', 720, '42')"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO communities (id, display_name, name) "
                    "VALUES (4, 'Waddlebot HQ', 'waddlebot_hq')"
                )
            )
            await conn.execute(
                sa_text("INSERT INTO reputation_global (hub_user_id, score) " "VALUES ('42', 600)")
            )
        result = await _run(dal, "!reputation", author_id="u-123")
        assert result is not None
        assert result.payload["text"] == (
            "\U0001f427 penguinzplays — Global: 600 (Trusted) · " "Waddlebot HQ: 720 (Respected)"
        )

    async def test_falls_back_to_display_name_when_no_author_id(self, dal: AsyncDB) -> None:
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_members "
                    "(community_id, platform, platform_user_id, display_name, reputation, user_id) "
                    "VALUES (4, 'twitch', NULL, 'penguinzplays', 655, NULL)"
                )
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO communities (id, display_name, name) "
                    "VALUES (4, 'Waddlebot HQ', 'waddlebot_hq')"
                )
            )
        result = await _run(dal, "!rep")
        assert result is not None
        assert "Waddlebot HQ: 655 (Trusted)" in result.payload["text"]
        assert "Global: 600 (Trusted)" in result.payload["text"]

    async def test_new_user_defaults_both_sides_to_600(self, dal: AsyncDB) -> None:
        """No `community_members` row and no `reputation_global` row -> 600 (Trusted) both sides."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO communities (id, display_name, name) "
                    "VALUES (4, 'Waddlebot HQ', 'waddlebot_hq')"
                )
            )
        result = await _run(dal, "!reputation", actor="stranger")
        assert result is not None
        assert result.payload["text"] == (
            "\U0001f427 stranger — Global: 600 (Trusted) · Waddlebot HQ: 600 (Trusted)"
        )

    async def test_community_without_display_name_falls_back_to_id(self, dal: AsyncDB) -> None:
        # No rows inserted - dal is empty
        result = await _run(dal, "!reputation", actor="stranger")
        assert result is not None
        assert "community 4: 600 (Trusted)" in result.payload["text"]

    async def test_missing_community_context_is_graceful(self, dal: AsyncDB) -> None:
        with bundle_context(tenant="acme", community=None, app_id="waddles.bot.twitch.default"):
            result = await transform(_event("!reputation"))
        assert result is not None
        assert "unavailable" in result.payload["text"]

    async def test_db_failure_is_swallowed_gracefully(self, dal: AsyncDB) -> None:
        """GUARDED: a DB error inside the bundle's own guard never crashes the bot."""
        # Close the DB to cause an error when querying
        await dal.close()
        result = await _run(dal, "!reputation")
        assert result is not None
        assert "unavailable" in result.payload["text"]


class TestReputationLabel:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (300, "Newcomer"),
            (464, "Newcomer"),
            (465, "Regular"),
            (574, "Regular"),
            (575, "Trusted"),
            (600, "Trusted"),  # REPUTATION_DEFAULT
            (657, "Trusted"),
            (658, "Respected"),
            (739, "Respected"),
            (740, "Champion"),
            (794, "Champion"),
            (795, "Legend"),
            (850, "Legend"),  # REPUTATION_MAX
        ],
    )
    def test_boundaries(self, dal: AsyncDB, score: int, expected: str) -> None:
        assert _reputation_label(score) == expected

    def test_tier_table_matches_hub_api(self, dal: AsyncDB) -> None:
        """Parses `hub_api`'s source directly (no import -- see module docstring) for parity.

        `hub_api` and this service are independently deployed processes
        with separate dependency trees and a colliding top-level
        `services` package name (both own one) -- a runtime cross-import
        would silently resolve to the wrong package, so this reads
        hub-api's module as plain text/AST instead of importing it.
        Skipped (not failed) only when the whole `hub_api` checkout is
        absent -- a genuinely different repo layout, not a drift signal;
        any AST/constant-shape problem *within* an existing checkout is a
        real failure, never swallowed.
        """
        hub_api_file = (
            Path(__file__).resolve().parents[3]
            / "hub_api"
            / "services"
            / "community_reputation_service.py"
        )
        if not hub_api_file.exists():
            pytest.skip(f"hub_api checkout not present at {hub_api_file}")

        tree = ast.parse(hub_api_file.read_text(encoding="utf-8"), filename=str(hub_api_file))
        hub_api_tiers = None
        for node in tree.body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "REPUTATION_TIERS"
                and node.value is not None
            ):
                hub_api_tiers = ast.literal_eval(node.value)
                break
        assert (
            hub_api_tiers is not None
        ), f"REPUTATION_TIERS not found in {hub_api_file} -- did it get renamed?"
        assert hub_api_tiers == REPUTATION_TIERS, (
            "bundle's REPUTATION_TIERS drifted from hub_api's -- keep the two mirrored copies "
            "(this file + hub_api/services/community_reputation_service.py) byte-identical"
        )
