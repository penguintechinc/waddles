"""Tests for `bundles.social_music_process.transform`."""

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

import bundles.social_music_process as social_music_process
from bundles.social_music_process import (
    _MUSIC_APP_ID,
    _PAUSE_RESUME_PERMISSION_DENIED_REPLY,
    _QUEUE_LINK_NOT_CONFIGURED,
    _SET_PERMISSION_DENIED_REPLY,
    _SET_UNAVAILABLE_REPLY,
    _SR_USAGE,
    _STATUS_CHECK_KEY,
    _STATUS_DISABLED_REPLY,
    _YOUTUBE_LABELS_LIMIT_REPLY,
    _YOUTUBE_LABELS_USAGE_REPLY,
    _public_webui_url,
    transform,
)

TENANT = "global"
COMMUNITY = "42"
COMMUNITY_ID = 42
APP_ID = "waddles.bot.discord.default"

#: Default test actor -- seeded as `moderator` in `_FakeDal` so ordinary
#: `!sr set youtube-labels` tests don't have to opt into permission
#: separately; `TestSetYoutubeLabelsPermission` uses a non-privileged
#: actor instead.
MOD_ACTOR = "test_user"
NON_MOD_ACTOR = "rando"


def _event(text: str, *, actor: str = MOD_ACTOR, **payload_overrides: object) -> PlatformEvent:
    """Create a test event with the given text; a channel_id + tokenized author_id by default."""
    payload: dict[str, object] = {
        "text": text,
        "channel_id": "123",
        "author_id": "platform-user-1",
        **payload_overrides,
    }
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


async def _seed_role_by_platform(dal: AsyncDB, platform_user_id: str, role: str) -> None:
    """Seed a `community_members` row matched by `(platform, platform_user_id)`."""
    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "INSERT INTO community_members "
                "(community_id, platform, platform_user_id, display_name, role) "
                "VALUES (:cid, 'discord', :puid, NULL, :role)"
            ),
            {"cid": COMMUNITY_ID, "puid": platform_user_id, "role": role},
        )


@pytest.fixture(autouse=True)
async def _dal() -> Any:
    """In-memory `penguin_dal.AsyncDB` -- `community_members`, `MOD_ACTOR` seeded as moderator.

    `_caller_is_moderator_or_admin`'s `raw_sql_rows()` calls (D21a) need a
    real SQLAlchemy engine, matched by `(platform, platform_user_id)` or
    `display_name`, same lookup convention `social_alias_process`'s own
    fixture uses.
    """
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, "
                "platform TEXT, platform_user_id TEXT, display_name TEXT, role TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "INSERT INTO community_members "
                "(community_id, platform, platform_user_id, display_name, role) "
                "VALUES (:cid, NULL, NULL, :name, 'moderator')"
            ),
            {"cid": COMMUNITY_ID, "name": MOD_ACTOR},
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
    monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_on)


@pytest.fixture(autouse=True)
def _reset_public_webui_url_cache() -> Any:
    """Clear `_public_webui_url`'s `lru_cache` around every test.

    The accessor is cached for process lifetime by design (module
    docstring), which would otherwise leak whichever `PUBLIC_WEBUI_URL`
    value/absence a prior test observed into this one -- e.g. the "unset"
    test's `None` result surviving into the "trailing slash" test.
    """
    _public_webui_url.cache_clear()
    yield
    _public_webui_url.cache_clear()


class TestTransformSongRequest:
    """Valid `!sr`/`!songrequest` parsing."""

    async def test_parses_sr_with_url(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr https://youtu.be/dQw4w9WgXcQ"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["music_query"] == "https://youtu.be/dQw4w9WgXcQ"
        assert result.payload["text"] == "https://youtu.be/dQw4w9WgXcQ"

    async def test_parses_songrequest_alias_with_free_text_query(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!songrequest never gonna give you up"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["music_query"] == "never gonna give you up"

    async def test_sets_target_app_id_for_cross_app_routing(self) -> None:
        """Mirrors the forum bundle's gh #298 routing mechanism -- see that bundle's test."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr some song"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID
        assert _MUSIC_APP_ID == "waddles.social.music.default"

    async def test_preserves_tokenized_requester_identity_and_channel(self) -> None:
        """`author_id` (tokenized platform id) and `channel_id` must survive untouched."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr some song"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["channel_id"] == "123"
        assert result.payload["author_id"] == "platform-user-1"
        assert result.actor == "test_user"

    async def test_case_insensitive(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!SR Some Song"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["music_query"] == "Some Song"

    async def test_whitespace_around_query_is_stripped(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr   some song   "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["music_query"] == "some song"


class TestTransformUsageHint:
    """Bad/empty arg -> usage-hint reply, never a crash."""

    async def test_bare_sr_returns_usage(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SR_USAGE
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_bare_songrequest_returns_usage(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!songrequest"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SR_USAGE

    async def test_whitespace_only_arg_returns_usage(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr      "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SR_USAGE


class TestTransformNonMatchingMessages:
    """Anything not `!sr`/`!songrequest` returns `None` -- no echo."""

    async def test_ordinary_chatter_returns_none(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("hello everyone")) is None

    async def test_other_commands_return_none(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!forum create x | y")) is None
            assert await transform(_event("!ping")) is None

    async def test_word_boundary_prevents_partial_match(self) -> None:
        """`!srx` must not be treated as `!sr` with a mangled arg."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!srx something")) is None

    async def test_empty_text_returns_none(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("")) is None
            assert await transform(_event("   ")) is None


class TestTransformFeatureFlag:
    """Flag OFF (or a flag/license outage, which `feature_enabled` itself degrades) -> `None`."""

    async def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!sr some song")) is None

    async def test_flag_off_still_ignores_non_matching_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag OFF must not change behavior for messages that were never `!sr` to begin with."""
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("hello")) is None

    async def test_flag_check_receives_tenant_and_community(
        self, monkeypatch: pytest.MonkeyPatch
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

        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _capture)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            await transform(_event("!sr some song"))

        assert captured["flag_key"] == "waddles.social.music"
        assert captured["tenant"] == TENANT
        assert captured["community"] == 42
        assert captured["default"] is True


class TestTransformStatus:
    """`!sr status` -- always answers, flag on or off."""

    async def test_status_enabled_routes_to_action_with_status_check_flag(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr status"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[_STATUS_CHECK_KEY] is True
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID

    async def test_status_alias_songrequest_also_routes(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!songrequest status"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[_STATUS_CHECK_KEY] is True

    async def test_status_case_insensitive(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr STATUS"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[_STATUS_CHECK_KEY] is True

    async def test_status_disabled_replies_directly_without_target_app_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one subcommand that must still answer when the flag is off."""
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr status"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _STATUS_DISABLED_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload
        assert _STATUS_CHECK_KEY not in result.payload

    async def test_status_preserves_channel_and_requester(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr status"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["channel_id"] == "123"
        assert result.payload["author_id"] == "platform-user-1"


class TestTransformSet:
    """`!sr set ...` -- only `youtube-labels` is implemented, never treated as a song title."""

    async def test_set_unknown_key_returns_unknown_setting_reply(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set discord #music"))
        assert isinstance(result, PlatformEvent)
        assert (
            result.payload["text"]
            == "song requests: unknown setting 'discord' — supported: youtube-labels"
        )
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload
        assert "music_query" not in result.payload

    async def test_set_unknown_key_names_key_as_typed(self) -> None:
        """The unknown-setting reply echoes the key as the caller typed it (not lowercased)."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set Discord #music"))
        assert isinstance(result, PlatformEvent)
        assert "'Discord'" in str(result.payload["text"])

    async def test_bare_set_returns_unavailable_reply(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SET_UNAVAILABLE_REPLY

    async def test_set_returns_none_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`set` follows the general flag gate (unlike `status`) -- silent when disabled."""
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!sr set discord #music")) is None

    async def test_a_song_literally_titled_settle_is_not_mistaken_for_set(self) -> None:
        """Word-boundary check: `settle down` must not match the `set` subcommand."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr settle down"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["music_query"] == "settle down"


class TestSetYoutubeLabels:
    """`!sr set youtube-labels <value>` -- parsing, limits, and outgoing event shape."""

    async def test_sets_labels_parsed_trimmed_lowercased(self) -> None:
        raw_text = "!sr set youtube-labels music,lofi, Chill"
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(raw_text))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "set"
        assert result.payload["key"] == "youtube_allowed_labels"
        assert result.payload["value"] == ["music", "lofi", "chill"]
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID
        assert result.payload["text"] == raw_text  # routing carries the original text through

    async def test_dedupes_labels(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music,Music,MUSIC,lofi"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == ["music", "lofi"]

    async def test_drops_empty_entries_from_doubled_commas(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music,,lofi,"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == ["music", "lofi"]

    async def test_none_clears_labels(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels none"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "set"
        assert result.payload["key"] == "youtube_allowed_labels"
        assert result.payload["value"] == []
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID

    async def test_clear_clears_labels(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels clear"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == []

    async def test_none_clear_case_insensitive(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels NONE"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == []

    async def test_no_value_returns_usage_reply(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _YOUTUBE_LABELS_USAGE_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_no_value_whitespace_only_returns_usage_reply(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels   "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _YOUTUBE_LABELS_USAGE_REPLY

    async def test_key_is_case_insensitive(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set YouTube-Labels music"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == ["music"]

    async def test_too_many_labels_returns_limit_reply(self) -> None:
        labels = ",".join(f"label{i}" for i in range(33))
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!sr set youtube-labels {labels}"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _YOUTUBE_LABELS_LIMIT_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_exactly_32_labels_is_allowed(self) -> None:
        labels = ",".join(f"label{i}" for i in range(32))
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!sr set youtube-labels {labels}"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == [f"label{i}" for i in range(32)]

    async def test_label_over_64_chars_returns_limit_reply(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!sr set youtube-labels {'x' * 65}"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _YOUTUBE_LABELS_LIMIT_REPLY

    async def test_label_exactly_64_chars_is_allowed(self) -> None:
        label = "x" * 64
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event(f"!sr set youtube-labels {label}"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["value"] == [label]

    async def test_preserves_tokenized_requester_identity_and_channel(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["channel_id"] == "123"
        assert result.payload["author_id"] == "platform-user-1"


class TestSetYoutubeLabelsPermission:
    """Admin/moderator gate on `!sr set youtube-labels` -- fail closed."""

    async def test_non_moderator_denied(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SET_PERMISSION_DENIED_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_moderator_allowed(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] != _SET_PERMISSION_DENIED_REPLY
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID

    async def test_allowed_by_platform_user_id_match(self, _dal: AsyncDB) -> None:
        await _seed_role_by_platform(_dal, "platform-user-1", "admin")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID

    async def test_role_lookup_error_fails_closed(
        self, _dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated permission lookup outage")

        monkeypatch.setattr("bundles.social_music_process.raw_sql_rows", _raise)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SET_PERMISSION_DENIED_REPLY

    async def test_unknown_key_does_not_require_permission(self) -> None:
        """An unrecognized `set` key replies before ever consulting the DAL."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set discord #music", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] != _SET_PERMISSION_DENIED_REPLY


class TestSetYoutubeLabelsFeatureFlag:
    """`waddles.social.music.youtube_labels` -- independent of `_FEATURE_FLAG`, default ON."""

    async def test_flag_off_falls_back_to_unavailable_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _music_flag_on(
            flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
        ) -> bool:
            return flag_key != "waddles.social.music.youtube_labels"

        monkeypatch.setattr(social_music_process, "feature_enabled", _music_flag_on)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr set youtube-labels music"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _SET_UNAVAILABLE_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_flag_checked_with_tenant_and_community(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _capture(
            flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
        ) -> bool:
            if flag_key == "waddles.social.music.youtube_labels":
                captured["tenant"] = tenant
                captured["community"] = community
                captured["default"] = default
            return True

        monkeypatch.setattr(social_music_process, "feature_enabled", _capture)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            await transform(_event("!sr set youtube-labels music"))

        assert captured["tenant"] == TENANT
        assert captured["community"] == 42
        assert captured["default"] is True


class TestTransformPauseResume:
    """`!sr pause`/`!sr resume` -- moderator/admin only, routed to the action stage on allow."""

    async def test_pause_allowed_emits_subcommand_and_target_app_id(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pause", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "pause"
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID
        assert "key" not in result.payload
        assert "value" not in result.payload

    async def test_resume_allowed_emits_subcommand_and_target_app_id(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr resume", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "resume"
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID
        assert "key" not in result.payload
        assert "value" not in result.payload

    async def test_pause_preserves_original_text_and_requester_identity(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pause", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "!sr pause"
        assert result.payload["channel_id"] == "123"
        assert result.payload["author_id"] == "platform-user-1"

    async def test_pause_case_insensitive(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!SR PAUSE", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "pause"

    async def test_pause_ignores_trailing_args(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pause extra", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "pause"
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID

    async def test_resume_ignores_trailing_args(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr resume please", actor=MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["subcommand"] == "resume"

    async def test_pause_non_moderator_denied(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pause", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PAUSE_RESUME_PERMISSION_DENIED_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_resume_non_moderator_denied(self) -> None:
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr resume", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PAUSE_RESUME_PERMISSION_DENIED_REPLY
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_pause_allowed_by_platform_user_id_match(self, _dal: AsyncDB) -> None:
        await _seed_role_by_platform(_dal, "platform-user-1", "admin")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pause", actor=NON_MOD_ACTOR))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _MUSIC_APP_ID

    async def test_pause_role_lookup_error_fails_closed(
        self, _dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated permission lookup outage")

        monkeypatch.setattr("bundles.social_music_process.raw_sql_rows", _raise)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pause"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _PAUSE_RESUME_PERMISSION_DENIED_REPLY

    async def test_pause_returns_none_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!sr pause", actor=MOD_ACTOR)) is None

    async def test_resume_returns_none_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!sr resume", actor=MOD_ACTOR)) is None

    async def test_a_song_literally_titled_pausing_is_not_mistaken_for_pause(self) -> None:
        """Word-boundary check: `pausing time` must not match the `pause` subcommand."""
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sr pausing time"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["music_query"] == "pausing time"


class TestTransformErrorHandling:
    """Missing/non-string `text` raises -- caught per-event by the process runner."""

    async def test_missing_text_field_raises(self) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="test_user",
            payload={"channel_id": "123"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event)

    async def test_non_string_text_raises(self) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="test_user",
            payload={"text": 123},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event)


class TestTransformSongQueue:
    """`!sq`/`!songqueue` -- a sibling command, own flag, own reply, no hub-api routing."""

    async def test_sq_replies_with_queue_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sq"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "song queue: https://waddles.example.com/c/42/music/queue"
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_songqueue_alias_replies_with_queue_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!songqueue"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "song queue: https://waddles.example.com/c/42/music/queue"

    async def test_sq_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!SQ"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "song queue: https://waddles.example.com/c/42/music/queue"

    async def test_sq_ignores_trailing_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`!sq <anything>` ignores the argument -- same reply as bare `!sq`."""
        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sq some random junk here"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "song queue: https://waddles.example.com/c/42/music/queue"

    async def test_unset_env_replies_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PUBLIC_WEBUI_URL", raising=False)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sq"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _QUEUE_LINK_NOT_CONFIGURED
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_unset_env_logs_a_warning_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The `lru_cache`d accessor body -- and its WARN log -- runs at most once."""
        monkeypatch.delenv("PUBLIC_WEBUI_URL", raising=False)
        with caplog.at_level("WARNING", logger="bundles.social_music_process"):
            with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
                await transform(_event("!sq"))
                await transform(_event("!songqueue"))
        warnings = [r for r in caplog.records if "public_webui_url_not_configured" in r.message]
        assert len(warnings) == 1

    async def test_trailing_slash_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com/")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!sq"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "song queue: https://waddles.example.com/c/42/music/queue"

    async def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _flag_off)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            assert await transform(_event("!sq")) is None

    async def test_flag_check_receives_queue_flag_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`!sq` checks its OWN flag key, independent of `!sr`'s `_FEATURE_FLAG`."""
        captured: dict[str, Any] = {}

        async def _capture(
            flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
        ) -> bool:
            captured["flag_key"] = flag_key
            captured["default"] = default
            return True

        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        monkeypatch.setattr("bundles.social_music_process.feature_enabled", _capture)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            await transform(_event("!sq"))

        assert captured["flag_key"] == "waddles.social.music.queue_page"
        assert captured["default"] is True


class TestBotProcessFeatureModuleRegistration:
    """`sq`/`songqueue` register onto this module in `bot_process._FEATURE_MODULES`.

    `test_bundles_bot_process.py` is owned by another agent this round --
    the registration/dispatch assertion lives here instead (see task scope).
    """

    def test_sq_and_songqueue_registered_to_this_module(self) -> None:
        import bundles.bot_process as bot_process

        assert bot_process._FEATURE_MODULES["sq"] == "bundles.social_music_process"
        assert bot_process._FEATURE_MODULES["songqueue"] == "bundles.social_music_process"

    async def test_sq_dispatches_through_bot_process_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`!sq` routes through `bot_process.transform` to this bundle, same as `!sr`."""
        import bundles.bot_process as bot_process

        monkeypatch.setenv("PUBLIC_WEBUI_URL", "https://waddles.example.com")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await bot_process.transform(_event("!sq"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "song queue: https://waddles.example.com/c/42/music/queue"
