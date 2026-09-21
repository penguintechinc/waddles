"""Tests for `bundles.bot_process`: real command+keyword bot transform.

Mirrors `test_bundles_echo_process.py`'s shape -- one `_event()` factory
building a `PlatformEvent`, one class per behavioral group. `TestRouter`
covers the board-demo command router (`bot_process.py`'s module docstring):
guarded import, guarded dispatch, and real dispatch to a sibling feature
bundle's `transform()`.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

import bundles.bot_process as bot_process
from bundles.bot_process import transform
from services.command_alias_store import CommandAlias


async def _alias_flag_on(*_args: Any, **_kwargs: Any) -> bool:
    return True


@pytest.fixture(autouse=True)
def _alias_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to the alias feature flag ON -- flag-off tests override this."""
    monkeypatch.setattr("bundles.bot_process.feature_enabled", _alias_flag_on)


async def _noop_transform(event: PlatformEvent) -> PlatformEvent | None:
    """Stand-in `TransformFn` for `!help` grouping tests -- never actually called."""
    return event


def _event(
    text: str,
    *,
    actor: str | None = "penguin",
    platform: str = "twitch",
    **payload_overrides: object,
) -> PlatformEvent:
    payload: dict[str, object] = {"text": text, **payload_overrides}
    return PlatformEvent(
        platform=platform,
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestCommands:
    async def test_ping(self) -> None:
        result = await transform(_event("!ping"))
        assert result is not None
        assert result.payload["text"] == "pong \U0001f427"

    @pytest.mark.parametrize("cmd", ["!hello", "!hi", "!hey"])
    async def test_hello_aliases(self, cmd: str) -> None:
        result = await transform(_event(cmd, actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "Hey penguin! \U0001f44b waddles is online."

    @pytest.mark.parametrize("cmd", ["!help", "!commands"])
    async def test_help_aliases(self, cmd: str) -> None:
        result = await transform(_event(cmd))
        assert result is not None
        assert result.payload["text"].startswith("Commands:")
        assert "!ping" in result.payload["text"]
        assert "!roll" in result.payload["text"]

    async def test_echo_with_text(self) -> None:
        result = await transform(_event("!echo hello there friend"))
        assert result is not None
        assert result.payload["text"] == "hello there friend"

    async def test_echo_without_text_returns_usage_hint(self) -> None:
        result = await transform(_event("!echo"))
        assert result is not None
        assert result.payload["text"] == "Usage: !echo <text>"

    async def test_echo_with_only_whitespace_returns_usage_hint(self) -> None:
        result = await transform(_event("!echo    "))
        assert result is not None
        assert result.payload["text"] == "Usage: !echo <text>"

    async def test_waddle_interpolates_platform(self) -> None:
        result = await transform(_event("!waddle", platform="discord"))
        assert result is not None
        assert result.payload["text"] == "\U0001f427 *waddles across discord*"

    async def test_roll_is_dice_range_and_uses_actor(self) -> None:
        with patch("bundles.bot_process.random.randint", return_value=4):
            result = await transform(_event("!roll", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "\U0001f3b2 penguin rolled a 4"

    async def test_flip_uses_mocked_choice(self) -> None:
        with patch("bundles.bot_process.random.choice", return_value="Heads"):
            result = await transform(_event("!flip"))
        assert result is not None
        assert result.payload["text"] == "\U0001fa99 Heads"

    async def test_unknown_command(self) -> None:
        """No community bound -- alias lookup is skipped, falls straight to unknown-command."""
        with bundle_context(tenant="acme", community=None, app_id="waddles.bot.twitch.default"):
            result = await transform(_event("!nonsense"))
        assert result is not None
        assert result.payload["text"] == "Unknown command. Try !help"

    async def test_commands_are_case_insensitive(self) -> None:
        result = await transform(_event("!PING"))
        assert result is not None
        assert result.payload["text"] == "pong \U0001f427"

    async def test_bare_bang_is_no_reply(self) -> None:
        assert await transform(_event("!")) is None


class TestNewInlineCommands:
    """Board-demo Fun/Utility/Community inline commands added to `_handle_command`."""

    @pytest.mark.parametrize("cmd", ["!dice", "!roll"])
    async def test_dice_is_alias_of_roll(self, cmd: str) -> None:
        with patch("bundles.bot_process.random.randint", return_value=3):
            result = await transform(_event(cmd, actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "\U0001f3b2 penguin rolled a 3"

    @pytest.mark.parametrize("cmd", ["!coin", "!flip"])
    async def test_coin_is_alias_of_flip(self, cmd: str) -> None:
        with patch("bundles.bot_process.random.choice", return_value="Tails"):
            result = await transform(_event(cmd))
        assert result is not None
        assert result.payload["text"] == "\U0001fa99 Tails"

    async def test_eight_ball_with_question(self) -> None:
        with patch("bundles.bot_process.random.choice", return_value="Signs point to yes."):
            result = await transform(_event("!8ball will the demo go well"))
        assert result is not None
        assert result.payload["text"] == "\U0001f3b1 Signs point to yes."

    async def test_eight_ball_without_question_returns_usage_hint(self) -> None:
        result = await transform(_event("!8ball"))
        assert result is not None
        assert result.payload["text"] == "Usage: !8ball <question>"

    async def test_hug_with_target(self) -> None:
        result = await transform(_event("!hug clubpenguinfan", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "penguin gives clubpenguinfan a warm hug! \U0001f917"

    async def test_hug_without_target(self) -> None:
        result = await transform(_event("!hug", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "penguin sends out a big hug! \U0001f917"

    async def test_love_with_target(self) -> None:
        with patch("bundles.bot_process.random.randint", return_value=87):
            result = await transform(_event("!love waddles", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "\U0001f495 penguin + waddles = 87% love match!"

    async def test_lurk(self) -> None:
        result = await transform(_event("!lurk", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "penguin slips into the shadows to lurk \U0001f440"

    async def test_followage_is_a_graceful_stub(self) -> None:
        result = await transform(_event("!followage", actor="penguin"))
        assert result is not None
        assert "coming soon" in result.payload["text"]

    async def test_uptime_reports_elapsed_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pin _START_TIME too -- the real (ambient) value from module import
        # combined with a mocked "now" can round to N-1 via float subtraction;
        # fixing both sides removes the dependency on real wall-clock state.
        monkeypatch.setattr("bundles.bot_process._START_TIME", 1_000.0)
        with patch("bundles.bot_process.time.monotonic", return_value=1_065.0):
            result = await transform(_event("!uptime"))
        assert result is not None
        assert result.payload["text"] == "waddles has been up for 1m 5s \U0001f427"

    async def test_uptime_reports_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bundles.bot_process._START_TIME", 1_000.0)
        with patch("bundles.bot_process.time.monotonic", return_value=4_725.0):
            result = await transform(_event("!uptime"))
        assert result is not None
        assert result.payload["text"] == "waddles has been up for 1h 2m 5s \U0001f427"

    async def test_uptime_reports_seconds_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bundles.bot_process._START_TIME", 1_000.0)
        with patch("bundles.bot_process.time.monotonic", return_value=1_009.0):
            result = await transform(_event("!uptime"))
        assert result is not None
        assert result.payload["text"] == "waddles has been up for 9s \U0001f427"

    async def test_time_reports_utc(self) -> None:
        result = await transform(_event("!time"))
        assert result is not None
        assert result.payload["text"].startswith("Server time: ")
        assert result.payload["text"].endswith(" UTC")

    async def test_rules(self) -> None:
        result = await transform(_event("!rules"))
        assert result is not None
        assert "Be kind" in result.payload["text"]

    @pytest.mark.parametrize("cmd", ["!bot", "!about"])
    async def test_bot_about(self, cmd: str) -> None:
        result = await transform(_event(cmd))
        assert result is not None
        assert "waddles" in result.payload["text"]

    async def test_socials(self) -> None:
        result = await transform(_event("!socials"))
        assert result is not None
        assert "twitter.com/waddlebot" in result.payload["text"]

    async def test_discord(self) -> None:
        result = await transform(_event("!discord"))
        assert result is not None
        assert "discord.gg/waddlebot" in result.payload["text"]


class TestKeywords:
    @pytest.mark.parametrize("greeting", ["hi", "hello", "hey", "yo", "sup", "howdy"])
    async def test_greeting_tokens(self, greeting: str) -> None:
        result = await transform(_event(f"{greeting} everyone", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "Hey penguin! \U0001f44b"

    async def test_greeting_is_word_boundary_not_substring(self) -> None:
        """`hiya` must NOT match the `hi` greeting token (word-boundary check)."""
        assert await transform(_event("hiya everyone")) is None

    async def test_waddle_mention_in_message(self) -> None:
        result = await transform(_event("I love waddles so much"))
        assert result is not None
        assert result.payload["text"] == "\U0001f427 someone say my name?"

    async def test_at_mention_of_bot(self) -> None:
        result = await transform(_event("@waddlebot are you there"))
        assert result is not None
        assert result.payload["text"] == "\U0001f427 someone say my name?"

    @pytest.mark.parametrize("thanks", ["thanks", "ty", "thank you"])
    async def test_thanks_variants(self, thanks: str) -> None:
        result = await transform(_event(f"{thanks} for the help", actor="penguin"))
        assert result is not None
        assert result.payload["text"] == "np, penguin! \U0001f427"

    async def test_random_chatter_is_no_reply(self) -> None:
        assert await transform(_event("just talking about the game")) is None


class TestRouter:
    """Board-demo command router: guarded import, guarded dispatch, real feature dispatch."""

    def test_load_feature_transforms_excludes_a_module_that_fails_to_import(self) -> None:
        """GUARDED IMPORT: one broken module is excluded; the rest still load."""
        real_import = bot_process.importlib.import_module

        def _flaky_import(name: str) -> object:
            if name == "bundles.social_quote_process":
                raise ImportError("simulated broken feature module")
            return real_import(name)

        with patch("bundles.bot_process.importlib.import_module", side_effect=_flaky_import):
            transforms = bot_process._load_feature_transforms()

        assert "quote" not in transforms
        assert {"alias", "poll", "announce", "forum"} <= transforms.keys()

    def test_load_feature_transforms_returns_empty_dict_when_every_import_fails(self) -> None:
        """GUARDED IMPORT, worst case: every feature import failing yields `{}`, not a raise."""
        with patch(
            "bundles.bot_process.importlib.import_module",
            side_effect=ImportError("simulated total feature outage"),
        ):
            transforms = bot_process._load_feature_transforms()

        assert transforms == {}

    async def test_bot_still_imports_and_ping_still_works_when_every_feature_import_fails(
        self,
    ) -> None:
        """`bot_process` module import + its own commands survive a total feature outage.

        `bot_process` is already successfully imported at module scope in
        this test file (proving "still imports"); this additionally proves
        `!ping` keeps working through the exact code path `transform()`
        uses, independent of whatever `_load_feature_transforms()` returns.
        """
        with patch(
            "bundles.bot_process.importlib.import_module",
            side_effect=ImportError("simulated total feature outage"),
        ):
            assert bot_process._load_feature_transforms() == {}

        result = await transform(_event("!ping"))
        assert result is not None
        assert result.payload["text"] == "pong \U0001f427"

    def test_help_text_omits_a_feature_that_never_loaded(self) -> None:
        """`!help` only advertises commands that actually loaded (never a broken one)."""
        text = bot_process._build_help_text({})
        assert text == (
            "Commands:\n"
            "Fun: !roll, !flip, !dice, !coin, !8ball <q>, !hug <user>, !love <user>, !lurk\n"
            "Utility: !ping, !hello, !help, !echo <text>, !waddle, !uptime, !time, !rules, !bot\n"
            "Community: !socials, !discord, !so <user>, !followage"
        )
        assert "!quote" not in text
        assert "Polls:" not in text

    def test_help_text_includes_loaded_features_grouped(self) -> None:
        """A loaded `poll` feature gets its own `Polls:` line; other features join Community."""
        loaded = dict.fromkeys(("quote", "poll", "reputation"), _noop_transform)
        text = bot_process._build_help_text(loaded)
        assert "Community: !socials, !discord, !so <user>, !followage, !quote, !reputation" in text
        assert "Polls: !poll" in text

    async def test_quote_dispatches_to_the_real_social_quote_bundle(self) -> None:
        """`!quote random` routes to `social_quote_process.transform`, not the bot's own reply."""
        set_bundle_dal(_EmptyQuoteDal())
        try:
            with bundle_context(tenant="acme", community="1", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!quote random"))
        finally:
            reset_bundle_dal_for_tests()
        assert result is not None
        assert result.payload["text"] == "No quotes found."

    async def test_poll_dispatches_to_the_real_community_polls_bundle(self) -> None:
        """Bare `!poll` routes to `community_polls_process.transform`'s own help text."""
        result = await transform(_event("!poll"))
        assert result is not None
        assert result.payload["text"].startswith("Poll commands:")

    async def test_chat_history_dispatches_to_the_real_community_chat_bundle(self) -> None:
        """`!chat-history` routes to `community_chat_process.transform`."""
        dal = await _empty_chat_dal()
        set_bundle_dal(dal)
        try:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!chat-history"))
        finally:
            reset_bundle_dal_for_tests()
            await dal.close()
        assert result is not None
        assert result.payload["text"] == "(no messages found)"

    async def test_channels_dispatches_to_the_real_community_chat_bundle(self) -> None:
        """`!channels` routes to `community_chat_process.transform`."""
        dal = await _empty_chat_dal()
        set_bundle_dal(dal)
        try:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!channels"))
        finally:
            reset_bundle_dal_for_tests()
            await dal.close()
        assert result is not None
        assert "general" in result.payload["text"]

    async def test_reputation_dispatches_to_the_real_reputation_bundle(self) -> None:
        """`!reputation` routes to `community_reputation_process.transform` with real DB data."""
        set_bundle_dal(_ReputationFoundDal())
        try:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!reputation", actor="penguinzplays"))
        finally:
            reset_bundle_dal_for_tests()
        assert result is not None
        assert result.payload["text"] == (
            "\U0001f427 penguinzplays — Global: 600 (Trusted) · waddlebot: 720 (Respected)"
        )

    async def test_rep_alias_dispatches_to_the_same_reputation_bundle(self) -> None:
        """`!rep` is a second command word routed to the same reputation module."""
        set_bundle_dal(_ReputationFoundDal())
        try:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!rep", actor="penguinzplays"))
        finally:
            reset_bundle_dal_for_tests()
        assert result is not None
        assert "waddlebot: 720 (Respected)" in result.payload["text"]

    async def test_reputation_dispatch_graceful_when_member_not_found(self) -> None:
        """No matching `community_members` row -- both scores default to the 600 baseline."""
        set_bundle_dal(_ReputationEmptyDal())
        try:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!reputation", actor="stranger"))
        finally:
            reset_bundle_dal_for_tests()
        assert result is not None
        assert result.payload["text"] == (
            "\U0001f427 stranger — Global: 600 (Trusted) · community 4: 600 (Trusted)"
        )

    async def test_reputation_lookup_failure_is_swallowed_gracefully(self) -> None:
        """A DB failure inside the reputation bundle's own guard never crashes the bot."""
        set_bundle_dal(_BoomDal())
        try:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!reputation", actor="penguin"))
        finally:
            reset_bundle_dal_for_tests()
        assert result is not None
        assert "unavailable" in result.payload["text"]

        # The bot's own commands are unaffected by the reputation bundle's DB failure.
        ping_result = await transform(_event("!ping"))
        assert ping_result is not None
        assert ping_result.payload["text"] == "pong \U0001f427"

    async def test_inventory_dispatches_to_the_real_inventory_bundle(self) -> None:
        """`!inventory` routes to `inventory_process.transform`'s own help text."""
        result = await transform(_event("!inventory"))
        assert result is not None
        assert result.payload["text"].startswith("Inventory commands:")

    async def test_feature_transform_raising_is_swallowed_with_graceful_reply(self) -> None:
        """GUARDED DISPATCH: a feature raising never propagates -- bot unaffected after."""

        async def _boom(event: PlatformEvent) -> PlatformEvent | None:
            raise RuntimeError("simulated feature bug")

        with patch.dict(bot_process._FEATURE_TRANSFORMS, {"quote": _boom}):
            result = await transform(_event("!quote random"))
            assert result is not None
            assert "snag" in result.payload["text"]

            # The bot's own commands are unaffected by the feature failure.
            ping_result = await transform(_event("!ping"))
            assert ping_result is not None
            assert ping_result.payload["text"] == "pong \U0001f427"

    async def test_non_command_chatter_still_returns_none_with_router_present(self) -> None:
        """Router presence doesn't change non-command chatter's no-reply behavior."""
        assert await transform(_event("just talking about the game")) is None


class TestCommandAliases:
    """Custom `command_aliases` expansion hook (`bot_process._expand_alias`)."""

    async def test_alias_rewrites_to_a_feature_command_with_remaining_args(self) -> None:
        """`!xx more` (alias `xx` -> `songrequest`) reaches the music feature with args `more`."""
        captured: dict[str, object] = {}

        async def _capture(event: PlatformEvent) -> PlatformEvent | None:
            captured["text"] = event.payload["text"]
            return dataclasses.replace(event, payload={**event.payload, "text": "captured"})

        with (
            patch.dict(bot_process._FEATURE_TRANSFORMS, {"songrequest": _capture}),
            patch(
                "bundles.bot_process.resolve_alias",
                AsyncMock(
                    return_value=CommandAlias(
                        alias="xx", target_command="songrequest", community_id=4
                    )
                ),
            ) as mock_resolve,
        ):
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!xx more"))

        assert captured["text"] == "!songrequest more"
        assert result is not None
        assert result.payload["text"] == "captured"
        mock_resolve.assert_awaited_once_with(community_id=4, alias="xx")

    async def test_alias_with_fixed_args_joins_with_remaining_user_args(self) -> None:
        """Alias `xx` -> `announce giveaway` + `!xx now` rewrites to `announce giveaway now`."""
        captured: dict[str, object] = {}

        async def _capture(event: PlatformEvent) -> PlatformEvent | None:
            captured["text"] = event.payload["text"]
            return dataclasses.replace(event, payload={**event.payload, "text": "captured"})

        with (
            patch.dict(bot_process._FEATURE_TRANSFORMS, {"announce": _capture}),
            patch(
                "bundles.bot_process.resolve_alias",
                AsyncMock(
                    return_value=CommandAlias(
                        alias="xx", target_command="announce giveaway", community_id=4
                    )
                ),
            ),
        ):
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!xx now"))

        assert captured["text"] == "!announce giveaway now"
        assert result is not None
        assert result.payload["text"] == "captured"

    async def test_known_built_in_command_never_triggers_a_lookup(self) -> None:
        """A recognized built-in command word skips the alias store entirely."""
        with patch("bundles.bot_process.resolve_alias", AsyncMock()) as mock_resolve:
            result = await transform(_event("!ping"))
        assert result is not None
        assert result.payload["text"] == "pong \U0001f427"
        mock_resolve.assert_not_awaited()

    async def test_known_feature_command_never_triggers_a_lookup(self) -> None:
        """A recognized feature command word skips the alias store entirely."""
        with patch("bundles.bot_process.resolve_alias", AsyncMock()) as mock_resolve:
            result = await transform(_event("!poll"))
        assert result is not None
        mock_resolve.assert_not_awaited()

    async def test_unknown_command_with_no_alias_behaves_like_before(self) -> None:
        """No matching alias row -- falls through to the normal unknown-command reply."""
        with patch("bundles.bot_process.resolve_alias", AsyncMock(return_value=None)):
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!nonsense"))
        assert result is not None
        assert result.payload["text"] == "Unknown command. Try !help"

    async def test_flag_off_skips_lookup_entirely(self) -> None:
        """Alias feature flag OFF -- behaves like an unrecognized command, no store call."""

        async def _flag_off(*_args: object, **_kwargs: object) -> bool:
            return False

        with (
            patch("bundles.bot_process.feature_enabled", _flag_off),
            patch("bundles.bot_process.resolve_alias", AsyncMock()) as mock_resolve,
        ):
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!nonsense"))
        assert result is not None
        assert result.payload["text"] == "Unknown command. Try !help"
        mock_resolve.assert_not_awaited()

    async def test_no_community_bound_skips_lookup_entirely(self) -> None:
        """A tenant-wide envelope (`community=None`) skips the alias lookup, no store call."""
        with patch("bundles.bot_process.resolve_alias", AsyncMock()) as mock_resolve:
            with bundle_context(tenant="acme", community=None, app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!nonsense"))
        assert result is not None
        assert result.payload["text"] == "Unknown command. Try !help"
        mock_resolve.assert_not_awaited()

    async def test_rewritten_target_unknown_falls_through_without_looping(self) -> None:
        """Alias expands to an unrecognized word -- no recursive re-lookup, just falls through."""
        with patch(
            "bundles.bot_process.resolve_alias",
            AsyncMock(return_value=CommandAlias(alias="xx", target_command="zzzz", community_id=4)),
        ) as mock_resolve:
            with bundle_context(tenant="acme", community="4", app_id="waddles.bot.twitch.default"):
                result = await transform(_event("!xx"))

        assert result is not None
        assert result.payload["text"] == "Unknown command. Try !help"
        mock_resolve.assert_awaited_once_with(community_id=4, alias="xx")


class _EmptyQuoteDal:
    """Minimal AsyncDAL stand-in -- `social_quote_process._fetch_random_quote` finds nothing."""

    async def execute(self, sql: str, params: list[object]) -> list[dict[str, object]]:
        return []


async def _empty_chat_dal() -> AsyncDB:
    """Real in-memory `penguin_dal.AsyncDB` with empty tables.

    `community_chat_process` finds no chat history/channels.

    `community_chat_process`'s two queries go through `raw_sql_rows()`
    (D21a), which needs a real SQLAlchemy engine -- a bare `.execute()`
    stand-in no longer suffices once the bundle-side call site changed.
    """
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text("CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id TEXT)")
        )
        await conn.execute(sa_text("CREATE TABLE tenants (id TEXT PRIMARY KEY)"))
        await conn.execute(
            sa_text(
                "CREATE TABLE hub_chat_messages ("
                "id INTEGER PRIMARY KEY, community_id INTEGER, channel_name TEXT, "
                "sender_username TEXT, message_content TEXT, message_type TEXT, "
                "created_at TEXT)"
            )
        )
    await db.reflect()
    return db


class _ReputationFoundDal:
    """Minimal AsyncDAL stand-in -- `community_reputation_process` finds a seeded member.

    Routes by SQL substring like `_FakeDal` in
    `test_bundles_community_reputation_process.py`: the member lookup (either
    the `platform_user_id` or `display_name` clause) returns a row carrying
    `hub_user_id`, the community label query returns a label, and the
    `reputation_global` query returns that hub user's global score.
    """

    async def execute(self, sql: str, params: list[object]) -> list[dict[str, object]]:
        if "reputation_global" in sql:
            return [{"score": 600}]
        if "FROM communities" in sql:
            return [{"label": "waddlebot"}]
        return [{"display_name": "penguinzplays", "reputation": 720, "hub_user_id": "42"}]


class _ReputationEmptyDal:
    """Minimal AsyncDAL stand-in -- `community_reputation_process` finds no member row."""

    async def execute(self, sql: str, params: list[object]) -> list[dict[str, object]]:
        return []


class _BoomDal:
    """Minimal AsyncDAL stand-in -- every query raises, simulating a DB outage."""

    async def execute(self, sql: str, params: list[object]) -> list[dict[str, object]]:
        raise RuntimeError("simulated DB outage")


class TestEdgeCases:
    async def test_empty_text_is_no_reply(self) -> None:
        assert await transform(_event("")) is None

    async def test_whitespace_only_text_is_no_reply(self) -> None:
        assert await transform(_event("   ")) is None

    async def test_missing_text_field_is_no_reply(self) -> None:
        event = PlatformEvent(
            platform="twitch",
            event_type="message",
            actor="penguin",
            payload={},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        assert await transform(event) is None

    async def test_missing_actor_falls_back_on_command(self) -> None:
        result = await transform(_event("!hello", actor=None))
        assert result is not None
        assert "friend" in result.payload["text"]

    async def test_missing_actor_falls_back_on_keyword(self) -> None:
        result = await transform(_event("hey team", actor=None))
        assert result is not None
        assert "friend" in result.payload["text"]

    async def test_payload_channel_id_survives_command_reply(self) -> None:
        result = await transform(_event("!ping", channel_id="chan-42", guild_id="guild-7"))
        assert result is not None
        assert result.payload["channel_id"] == "chan-42"
        assert result.payload["guild_id"] == "guild-7"

    async def test_payload_channel_id_survives_keyword_reply(self) -> None:
        result = await transform(_event("hello there", channel_id="chan-42"))
        assert result is not None
        assert result.payload["channel_id"] == "chan-42"

    async def test_top_level_fields_preserved(self) -> None:
        event = _event("!ping", platform="discord")
        result = await transform(event)
        assert result is not None
        assert result.platform == "discord"
        assert result.event_type == "message"
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"

    async def test_original_event_is_not_mutated(self) -> None:
        event = _event("!ping")
        result = await transform(event)
        assert event.payload["text"] == "!ping"
        assert result is not event
