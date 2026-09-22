"""Tests for `bundles.social_shoutout_process.transform` -- gh #316 process half.

Mirrors `test_bundles_social_music_process.py`'s shape: one `_event()`
factory, an in-memory `penguin_dal.AsyncDB` seeded per test standing in for
both `shoutout_config` and `community_members` reads (D21a -- the bundle's
`raw_sql_rows()` calls need a real SQLAlchemy engine, not a bare mock), one
class per behavioral group. `TestBotProcessFeatureModuleRegistration` at the
bottom covers `bot_process._FEATURE_MODULES` registration + real dispatch,
following that same sibling file's precedent for a bundle whose own
`test_bundles_bot_process.py` is scoped to another agent this round (see
task scope) -- only the joke-reply assertions were touched there.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core import (
    PROCESS_TARGET_APP_ID_KEY,
    PlatformEvent,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

from bundles.social_shoutout_process import (
    _INVALID_LOGIN_REPLY,
    _PERMISSION_DENIED_REPLY,
    _SELF_SHOUTOUT_REPLY,
    _SHOUTOUT_APP_ID,
    _SO_USAGE,
    _VSO_USAGE,
    transform,
)

TENANT = "global"
COMMUNITY = "42"
COMMUNITY_ID = 42
APP_ID = "waddles.bot.twitch.default"

#: Default test actor -- seeded as `moderator` in the `dal` fixture so
#: `mod`-gated tests don't have to opt into permission separately.
MOD_ACTOR = "test_user"
NON_MOD_ACTOR = "rando"
ADMIN_ACTOR = "the_owner"


def _event(
    text: str, *, actor: str | None = MOD_ACTOR, **payload_overrides: object
) -> PlatformEvent:
    """Create a test event with the given text; a channel_id + tokenized author_id by default."""
    payload: dict[str, object] = {
        "text": text,
        "channel_id": "123",
        "author_id": "platform-user-1",
        **payload_overrides,
    }
    return PlatformEvent(
        platform="twitch",
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


async def _set_config(dal: AsyncDB, *, so_permission: str, vso_permission: str) -> None:
    """Replace the seeded `shoutout_config` row for `COMMUNITY_ID`."""
    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text("DELETE FROM shoutout_config WHERE community_id = :cid"),
            {"cid": COMMUNITY_ID},
        )
        await conn.execute(
            sa_text(
                "INSERT INTO shoutout_config (community_id, so_permission, vso_permission) "
                "VALUES (:cid, :so, :vso)"
            ),
            {"cid": COMMUNITY_ID, "so": so_permission, "vso": vso_permission},
        )


async def _clear_config(dal: AsyncDB) -> None:
    """Remove the seeded `shoutout_config` row -- simulates `has_config_row=False`."""
    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text("DELETE FROM shoutout_config WHERE community_id = :cid"),
            {"cid": COMMUNITY_ID},
        )


async def _seed_role_by_platform(dal: AsyncDB, platform_user_id: str, role: str) -> None:
    """Seed a `community_members` row matched by `(platform, platform_user_id)`."""
    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "INSERT INTO community_members "
                "(community_id, platform, platform_user_id, display_name, role) "
                "VALUES (:cid, 'twitch', :puid, NULL, :role)"
            ),
            {"cid": COMMUNITY_ID, "puid": platform_user_id, "role": role},
        )


@pytest.fixture
async def dal() -> Any:
    """In-memory `penguin_dal.AsyncDB` with `shoutout_config`/`community_members`.

    Seeds the default fixture roles (`MOD_ACTOR` -> moderator, `ADMIN_ACTOR`
    -> admin, matched by `display_name`) and a `mod`/`mod` config row for
    `COMMUNITY_ID`, matching the old `_FakeDal.__init__`'s defaults.
    """
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE shoutout_config ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, "
                "so_permission TEXT, vso_permission TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, "
                "platform TEXT, platform_user_id TEXT, display_name TEXT, role TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "INSERT INTO shoutout_config (community_id, so_permission, vso_permission) "
                "VALUES (:cid, 'mod', 'mod')"
            ),
            {"cid": COMMUNITY_ID},
        )
        await conn.execute(
            sa_text(
                "INSERT INTO community_members (community_id, platform, platform_user_id, "
                "display_name, role) VALUES (:cid, NULL, NULL, :name, 'moderator')"
            ),
            {"cid": COMMUNITY_ID, "name": MOD_ACTOR},
        )
        await conn.execute(
            sa_text(
                "INSERT INTO community_members (community_id, platform, platform_user_id, "
                "display_name, role) VALUES (:cid, NULL, NULL, :name, 'admin')"
            ),
            {"cid": COMMUNITY_ID, "name": ADMIN_ACTOR},
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


async def _flag_on(*_args: Any, **_kwargs: Any) -> bool:
    return True


async def _flag_off(*_args: Any, **_kwargs: Any) -> bool:
    return False


@pytest.fixture(autouse=True)
def _flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to flag ON -- the OFF-specific tests override this explicitly."""
    monkeypatch.setattr("bundles.social_shoutout_process.feature_enabled", _flag_on)


class TestTransformShoutout:
    """Valid `!so`/`!shoutout`/`!vso` parsing -- both commands, aliases, payload shape."""

    @pytest.mark.parametrize("cmd", ["!so", "!shoutout"])
    async def test_text_shoutout_aliases_produce_kind_text(self, dal: AsyncDB, cmd: str) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"{cmd} clubpenguinfan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "shoutout"
        assert result.payload["kind"] == "text"
        assert result.payload["target"] == "clubpenguinfan"

    async def test_vso_produces_kind_video(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!vso clubpenguinfan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["kind"] == "video"
        assert result.payload["target"] == "clubpenguinfan"

    async def test_strips_leading_at_and_lowercases(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so @ClubPenguinFan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["target"] == "clubpenguinfan"

    async def test_command_prefix_is_case_insensitive(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!SO clubpenguinfan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["target"] == "clubpenguinfan"

    async def test_sets_target_app_id_for_cross_app_routing(self, dal: AsyncDB) -> None:
        """Mirrors the forum/music bundles' gh #298 routing mechanism (see those bundles' tests)."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so clubpenguinfan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID
        assert _SHOUTOUT_APP_ID == "waddles.bot.shoutout.default"

    async def test_preserves_tokenized_requester_identity_and_channel(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so clubpenguinfan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["channel_id"] == "123"
        assert result.payload["author_id"] == "platform-user-1"
        assert result.actor == MOD_ACTOR


class TestTransformUsageHint:
    """Missing/blank target -> usage-hint reply, never a crash; no cross-app routing."""

    async def test_bare_so_returns_usage(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SO_USAGE
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_bare_shoutout_alias_also_uses_so_usage(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!shoutout"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SO_USAGE

    async def test_bare_vso_returns_vso_usage(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!vso"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _VSO_USAGE

    async def test_whitespace_only_target_returns_usage(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so      "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SO_USAGE


class TestTransformInvalidLogin:
    """Target failing `^[a-z0-9_]{3,25}$` (post-normalization) -> invalid-login reply."""

    async def test_too_short_is_invalid(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so ab"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _INVALID_LOGIN_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_too_long_is_invalid(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!so {'a' * 26}"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _INVALID_LOGIN_REPLY

    async def test_invalid_character_is_invalid(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so club-penguin-fan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _INVALID_LOGIN_REPLY

    async def test_minimum_length_is_valid(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so abc"))
        assert isinstance(result, PlatformEvent)
        assert result.payload.get("target") == "abc"

    async def test_maximum_length_is_valid(self, dal: AsyncDB) -> None:
        target = "a" * 25
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!so {target}"))
        assert isinstance(result, PlatformEvent)
        assert result.payload.get("target") == target


class TestTransformSelfShoutout:
    """`target == caller` (both normalized) -> denied regardless of permission level."""

    async def test_self_shoutout_denied(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!so {MOD_ACTOR}", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SELF_SHOUTOUT_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_self_shoutout_denied_case_and_at_insensitive(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so @Test_User", actor="test_user"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SELF_SHOUTOUT_REPLY

    async def test_self_shoutout_denied_even_for_admin(self, dal: AsyncDB) -> None:
        """Self-shoutout is a flat rule -- not overridden by an elevated role."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!so {ADMIN_ACTOR}", actor=ADMIN_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SELF_SHOUTOUT_REPLY

    async def test_different_target_is_not_self_shoutout(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so someone_else", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] != _SELF_SHOUTOUT_REPLY


class TestTransformPermission:
    """`shoutout_config.so_permission`/`vso_permission` gates who may shout out."""

    async def test_everyone_permission_allows_non_mod(self, dal: AsyncDB) -> None:
        await _set_config(dal, so_permission="everyone", vso_permission="mod")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_vip_permission_degrades_to_everyone(self, dal: AsyncDB) -> None:
        """No badge data on `PlatformEvent` -- `vip` is unenforceable, degrades to always-allow."""
        await _set_config(dal, so_permission="vip", vso_permission="mod")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_subscriber_permission_degrades_to_everyone(self, dal: AsyncDB) -> None:
        await _set_config(dal, so_permission="mod", vso_permission="subscriber")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!vso target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_mod_permission_denies_non_mod(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_mod_permission_allows_moderator(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_admin_only_permission_denies_moderator(self, dal: AsyncDB) -> None:
        """`admin_only` is a stricter tier than `mod` -- a plain moderator is still denied."""
        await _set_config(dal, so_permission="admin_only", vso_permission="mod")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    async def test_admin_only_permission_allows_admin(self, dal: AsyncDB) -> None:
        await _set_config(dal, so_permission="admin_only", vso_permission="mod")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=ADMIN_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_unknown_permission_value_falls_back_to_mod_threshold(self, dal: AsyncDB) -> None:
        await _set_config(dal, so_permission="some_future_tier", vso_permission="mod")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            denied = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
            allowed = await transform(_event("!so target_user", actor=MOD_ACTOR))
        assert isinstance(denied, PlatformEvent)
        assert denied.payload["text"] == _PERMISSION_DENIED_REPLY
        assert isinstance(allowed, PlatformEvent)
        assert allowed.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_missing_config_row_defaults_to_mod(self, dal: AsyncDB) -> None:
        await _clear_config(dal)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            denied = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
            allowed = await transform(_event("!so target_user", actor=MOD_ACTOR))
        assert isinstance(denied, PlatformEvent)
        assert denied.payload["text"] == _PERMISSION_DENIED_REPLY
        assert isinstance(allowed, PlatformEvent)
        assert allowed.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_config_lookup_error_defaults_to_mod(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated shoutout_config outage")

        monkeypatch.setattr("bundles.social_shoutout_process.raw_sql_rows", _raise)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    async def test_role_lookup_error_fails_closed(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_raw_sql_rows = __import__(
            "bundles.social_shoutout_process", fromlist=["raw_sql_rows"]
        ).raw_sql_rows

        async def _selective_raise(dal_param: Any, sql: str, params: Any = None) -> Any:
            if "shoutout_config" in sql:
                return await original_raw_sql_rows(dal_param, sql, params)
            raise RuntimeError("simulated permission lookup outage")

        monkeypatch.setattr("bundles.social_shoutout_process.raw_sql_rows", _selective_raise)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    async def test_allowed_by_platform_user_id_match(self, dal: AsyncDB) -> None:
        await _seed_role_by_platform(dal, "platform-user-1", "admin")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID

    async def test_no_community_defaults_to_mod_and_denies_by_default(self, dal: AsyncDB) -> None:
        """A tenant-wide envelope (`community=None`) has no config/role to read -- fails closed."""
        with bundle_context(tenant=TENANT, community=None, app_id=APP_ID):
            result = await transform(_event("!so target_user", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY


class TestTransformFeatureFlag:
    """Flag OFF (or a flag/license outage, which `feature_enabled` itself degrades) -> `None`."""

    @pytest.mark.parametrize("cmd", ["!so", "!shoutout", "!vso"])
    async def test_flag_off_returns_none(
        self, dal: AsyncDB, cmd: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bundles.social_shoutout_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event(f"{cmd} clubpenguinfan")) is None

    async def test_flag_off_still_ignores_non_matching_messages(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bundles.social_shoutout_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("hello")) is None

    async def test_flag_check_receives_tenant_and_community(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _capture(
            flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
        ) -> bool:
            captured["flag_key"] = flag_key
            captured["tenant"] = tenant
            captured["community"] = community
            captured["default"] = default
            return True

        monkeypatch.setattr("bundles.social_shoutout_process.feature_enabled", _capture)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            await transform(_event("!so clubpenguinfan"))

        assert captured["flag_key"] == "waddles.bot.shoutout"
        assert captured["tenant"] == TENANT
        assert captured["community"] == 42
        assert captured["default"] is True


class TestTransformNonMatchingMessages:
    """Anything not `!so`/`!shoutout`/`!vso` returns `None` -- no echo."""

    async def test_ordinary_chatter_returns_none(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("hello everyone")) is None

    async def test_other_commands_return_none(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!forum create x | y")) is None
            assert await transform(_event("!sr some song")) is None
            assert await transform(_event("!ping")) is None

    async def test_word_boundary_prevents_partial_match(self, dal: AsyncDB) -> None:
        """`!sox`/`!vsox` must not be treated as `!so`/`!vso` with a mangled arg."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!sox something")) is None
            assert await transform(_event("!vsox something")) is None

    async def test_empty_text_returns_none(self, dal: AsyncDB) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("")) is None
            assert await transform(_event("   ")) is None


class TestTransformErrorHandling:
    """Missing/non-string `text` raises -- caught per-event by the process runner."""

    async def test_missing_text_field_raises(self, dal: AsyncDB) -> None:
        event = PlatformEvent(
            platform="twitch",
            event_type="message",
            actor=MOD_ACTOR,
            payload={},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event)

    async def test_non_string_text_raises(self, dal: AsyncDB) -> None:
        event = PlatformEvent(
            platform="twitch",
            event_type="message",
            actor=MOD_ACTOR,
            payload={"text": 123},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event)


class TestBotProcessFeatureModuleRegistration:
    """`so`/`shoutout`/`vso` register onto this module in `bot_process._FEATURE_MODULES`.

    `test_bundles_bot_process.py` is scoped to another agent this round
    (see task scope) -- only its joke-reply assertions were removed there;
    the registration/dispatch assertion lives here instead, mirroring
    `test_bundles_social_music_process.py::TestBotProcessFeatureModuleRegistration`'s
    identical precedent for `sq`/`songqueue`.
    """

    def test_so_shoutout_vso_registered_to_this_module(self) -> None:
        import bundles.bot_process as bot_process

        assert bot_process._FEATURE_MODULES["so"] == "bundles.social_shoutout_process"
        assert bot_process._FEATURE_MODULES["shoutout"] == "bundles.social_shoutout_process"
        assert bot_process._FEATURE_MODULES["vso"] == "bundles.social_shoutout_process"

    def test_so_and_shoutout_are_no_longer_bot_builtins(self) -> None:
        """The old hardcoded joke branch is gone -- feature dispatch owns these words now."""
        import bundles.bot_process as bot_process

        assert "so" not in bot_process._BOT_COMMANDS
        assert "shoutout" not in bot_process._BOT_COMMANDS

    async def test_so_dispatches_through_bot_process_router(self, dal: AsyncDB) -> None:
        """`!so` routes through `bot_process.transform` to this bundle, not the removed joke."""
        import bundles.bot_process as bot_process

        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await bot_process.transform(_event("!so clubpenguinfan"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["target"] == "clubpenguinfan"
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _SHOUTOUT_APP_ID
