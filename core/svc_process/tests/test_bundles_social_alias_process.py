"""Tests for `bundles.social_alias_process.transform` -- alias set/list/remove/invoke.

`_lookup_alias`/`_upsert_alias`/`_soft_delete_alias`/`_list_aliases` go
through `penguin_dal`'s own query builder (D21a) and
`_caller_is_moderator_or_admin` through `raw_sql_rows()`, so the fixture
below is a real in-memory SQLite `penguin_dal.AsyncDB` with `command_aliases`
+ `community_members`, seeded per test, rather than a bare dict-backed mock.
A SQLAlchemy `before_cursor_execute` engine listener replaces the old
`_FakeDal._select_count` instrumentation for the "this code path never
touches the DB at all" assertions.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from penguin_dal import AsyncDB
from sqlalchemy import event
from sqlalchemy import text as sa_text

import bundles.social_alias_process as social_alias_process
import services.command_alias_store as command_alias_store_module
from bundles.social_alias_process import (
    _ALIAS_USAGE,
    _COMMUNITY_REQUIRED_MSG,
    _NO_ALIASES_MSG,
    _PERMISSION_DENIED_MSG,
    transform,
)

TENANT = "acme"
COMMUNITY = "1"
COMMUNITY_ID = 1
COMMUNITY_2 = "2"
APP_ID = "waddles.social.alias.default"

#: Default test actor -- seeded as `moderator` in the `dal` fixture so
#: ordinary set/list/remove tests don't have to opt into permission
#: separately; dedicated `TestPermissions` tests use a non-privileged actor.
MOD_ACTOR = "penguin"
NON_MOD_ACTOR = "rando"


def _event(
    text: str, *, actor: str | None = MOD_ACTOR, **payload_overrides: object
) -> PlatformEvent:
    payload: dict[str, object] = {
        "text": text,
        "channel_id": "chan-123",
        **payload_overrides,
    }
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor=actor,
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


async def _get_alias(dal: AsyncDB, alias_id: int) -> dict[str, Any] | None:
    """Fetch one `command_aliases` row by id as a plain dict, or `None`."""
    async with dal.engine.connect() as conn:
        result = await conn.execute(
            sa_text("SELECT * FROM command_aliases WHERE id = :id"), {"id": alias_id}
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None


async def _clear_aliases(dal: AsyncDB) -> None:
    """Delete every seeded `command_aliases` row (simulates `_aliases = {}`)."""
    async with dal.engine.begin() as conn:
        await conn.execute(sa_text("DELETE FROM command_aliases"))


async def _seed_aliases(dal: AsyncDB, rows: list[dict[str, Any]]) -> None:
    """Replace all `command_aliases` rows with the given seed set."""
    await _clear_aliases(dal)
    async with dal.engine.begin() as conn:
        for row in rows:
            await conn.execute(
                sa_text(
                    "INSERT INTO command_aliases "
                    "(id, community_id, alias, target_command, usage_count, deleted_at, "
                    "created_by) "
                    "VALUES (:id, :community_id, :alias, :target_command, :usage_count, "
                    ":deleted_at, :created_by)"
                ),
                {
                    "id": row["id"],
                    "community_id": row["community_id"],
                    "alias": row["alias"],
                    "target_command": row["target_command"],
                    "usage_count": row.get("usage_count", 0),
                    "deleted_at": row.get("deleted_at"),
                    "created_by": row.get("created_by", "penguin"),
                },
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


async def _flag_on(*_args: Any, **_kwargs: Any) -> bool:
    return True


@pytest.fixture(autouse=True)
def _flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to flag ON -- `TestFeatureFlag` overrides this explicitly.

    Mocked (not exercising the real entitlement client) so the suite never
    depends on PostHog/license-server reachability, matching
    `community_context_process`'s own test convention.
    """
    monkeypatch.setattr(social_alias_process, "feature_enabled", _flag_on)


@pytest.fixture(autouse=True)
async def dal() -> Any:
    """In-memory `penguin_dal.AsyncDB` -- `command_aliases` + `community_members`.

    Seeds one active alias (`greet` -> `hello {user} {args}`, community 1)
    and `MOD_ACTOR` as a moderator, matching the old `_FakeDal.__init__`'s
    defaults. `select_count` (via an engine listener attached AFTER
    seeding) replaces `_FakeDal._select_count`.
    """
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE command_aliases ("
                "id INTEGER PRIMARY KEY, community_id INTEGER, alias TEXT, "
                "target_command TEXT, usage_count INTEGER DEFAULT 0, deleted_at TEXT, "
                "created_by TEXT)"
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
                "INSERT INTO command_aliases "
                "(id, community_id, alias, target_command, usage_count, deleted_at, created_by) "
                "VALUES (1, :cid, 'greet', 'hello {user} {args}', 5, NULL, 'penguin')"
            ),
            {"cid": COMMUNITY_ID},
        )
        await conn.execute(
            sa_text(
                "INSERT INTO community_members "
                "(community_id, platform, platform_user_id, display_name, role) "
                "VALUES (:cid, NULL, NULL, :name, 'moderator')"
            ),
            {"cid": COMMUNITY_ID, "name": MOD_ACTOR},
        )
        # Also a moderator in community 2 -- `TestCrossCommunityIsolation` tests
        # alias-table isolation specifically, not permission-role isolation,
        # so MOD_ACTOR needs standing in both communities.
        await conn.execute(
            sa_text(
                "INSERT INTO community_members "
                "(community_id, platform, platform_user_id, display_name, role) "
                "VALUES (2, NULL, NULL, :name, 'moderator')"
            ),
            {"name": MOD_ACTOR},
        )
    await db.reflect()

    db.select_count = 0

    def _count(*_args: Any, **_kwargs: Any) -> None:
        db.select_count += 1

    event.listens_for(db.engine.sync_engine, "before_cursor_execute")(_count)

    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


@pytest.fixture(autouse=True)
def _invalidate_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Stub `services.command_alias_store.invalidate_alias` -- writes must never hit real Redis."""
    calls: list[tuple[int, str]] = []

    async def _fake_invalidate(*, community_id: int, alias: str, redis_client: Any = None) -> None:
        calls.append((community_id, alias))

    monkeypatch.setattr(command_alias_store_module, "invalidate_alias", _fake_invalidate)
    return calls


async def _run(
    text: str,
    *,
    community: str | None = COMMUNITY,
    actor: str | None = MOD_ACTOR,
    **overrides: object,
) -> PlatformEvent | None:
    with bundle_context(tenant=TENANT, community=community, app_id=APP_ID):
        return await transform(_event(text, actor=actor, **overrides))


class TestFeatureFlag:
    async def test_flag_off_returns_none_no_reply(
        self, monkeypatch: pytest.MonkeyPatch, dal: AsyncDB
    ) -> None:
        async def _flag_off(*_a: Any, **_kw: Any) -> bool:
            return False

        monkeypatch.setattr(social_alias_process, "feature_enabled", _flag_off)
        result = await _run("!alias list")
        assert result is None
        assert dal.select_count == 0


class TestRoutingAndMalformedEvents:
    async def test_non_command_chatter_returns_none(self) -> None:
        assert await _run("just chatting") is None

    async def test_command_without_bang_returns_none(self) -> None:
        assert await _run("greet penguin") is None

    async def test_missing_text_raises(self) -> None:
        event_ = PlatformEvent(
            platform="discord", event_type="message", actor=None, payload={}, occurred_at="x"
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event_)

    async def test_empty_text_raises(self) -> None:
        event_ = PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload={"text": ""},
            occurred_at="x",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event_)

    async def test_text_is_not_string_raises(self) -> None:
        event_ = PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload={"text": 123},
            occurred_at="x",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event_)

    async def test_whitespace_only_text_raises(self) -> None:
        event_ = PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload={"text": "   "},
            occurred_at="x",
        )
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="text"):
                await transform(event_)


class TestList:
    async def test_bare_alias_lists(self) -> None:
        result = await _run("!alias")
        assert result is not None
        assert result.payload["text"] == "aliases: !greet → !hello {user} {args}"

    async def test_alias_list_word_lists(self) -> None:
        result = await _run("!alias list")
        assert result is not None
        assert result.payload["text"] == "aliases: !greet → !hello {user} {args}"

    async def test_list_case_insensitive(self) -> None:
        result = await _run("!ALIAS LIST")
        assert result is not None
        assert result.payload["text"] == "aliases: !greet → !hello {user} {args}"

    async def test_list_empty_returns_no_aliases_message(self, dal: AsyncDB) -> None:
        await _clear_aliases(dal)
        result = await _run("!alias list")
        assert result is not None
        assert result.payload["text"] == _NO_ALIASES_MSG

    async def test_list_sorted_and_truncated_past_15(self, dal: AsyncDB) -> None:
        await _seed_aliases(
            dal,
            [
                {
                    "id": i,
                    "community_id": COMMUNITY_ID,
                    "alias": f"a{i:02d}",
                    "target_command": "ping",
                    "deleted_at": None,
                    "usage_count": 0,
                }
                for i in range(20)
            ],
        )
        result = await _run("!alias list")
        assert result is not None
        text = result.payload["text"]
        assert text.startswith("aliases: !a00 → !ping, !a01 → !ping")
        assert text.endswith("…and 5 more")

    async def test_list_without_community_returns_guard(self, dal: AsyncDB) -> None:
        result = await _run("!alias list", community=None)
        assert result is not None
        assert result.payload["text"] == _COMMUNITY_REQUIRED_MSG
        assert dal.select_count == 0

    async def test_list_handles_db_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Test error")

        monkeypatch.setattr(social_alias_process, "_list_aliases", _raise)
        result = await _run("!alias list")
        assert result is not None
        assert "Failed to list aliases" in result.payload["text"]


class TestSetAlias:
    async def test_positional_set_success(self, dal: AsyncDB, _invalidate_calls: list[Any]) -> None:
        result = await _run("!alias newcmd echo hi there")
        assert result is not None
        assert result.payload["text"] == "alias set: !newcmd → !echo hi there"
        assert _invalidate_calls == [(1, "newcmd")]

    async def test_add_synonym_success(self, dal: AsyncDB) -> None:
        result = await _run("!alias add newcmd echo hi there")
        assert result is not None
        assert result.payload["text"] == "alias set: !newcmd → !echo hi there"

    async def test_set_lowercases_name(self, dal: AsyncDB) -> None:
        result = await _run("!alias NewCmd echo hi")
        assert result is not None
        assert result.payload["text"] == "alias set: !newcmd → !echo hi"

    async def test_set_overwrites_existing_active_alias(self, dal: AsyncDB) -> None:
        result = await _run("!alias greet echo hi")
        assert result is not None
        assert result.payload["text"] == "alias set: !greet → !echo hi"
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["target_command"] == "echo hi"

    async def test_set_revives_soft_deleted_alias(self, dal: AsyncDB) -> None:
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text("UPDATE command_aliases SET deleted_at = :d WHERE id = 1"),
                {"d": "2026-01-01T00:00:00+00:00"},
            )
        result = await _run("!alias greet echo hi")
        assert result is not None
        assert result.payload["text"] == "alias set: !greet → !echo hi"
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is None

    async def test_missing_args_add_only_returns_usage(self) -> None:
        result = await _run("!alias add")
        assert result is not None
        assert result.payload["text"] == _ALIAS_USAGE

    async def test_missing_expansion_positional_returns_usage(self) -> None:
        result = await _run("!alias newcmd")
        assert result is not None
        assert result.payload["text"] == _ALIAS_USAGE

    async def test_missing_expansion_add_returns_usage(self) -> None:
        result = await _run("!alias add newcmd")
        assert result is not None
        assert result.payload["text"] == _ALIAS_USAGE

    async def test_invalid_name_rejected(self) -> None:
        result = await _run("!alias greet@me echo hi")
        assert result is not None
        assert "alias names are letters, numbers, - and _ (max 32)" in result.payload["text"]

    async def test_name_allows_hyphen_and_underscore(self, dal: AsyncDB) -> None:
        result = await _run("!alias my-new_cmd echo hi")
        assert result is not None
        assert result.payload["text"] == "alias set: !my-new_cmd → !echo hi"

    async def test_name_too_long_rejected(self) -> None:
        long_name = "a" * 33
        result = await _run(f"!alias {long_name} echo hi")
        assert result is not None
        assert "alias names are letters, numbers, - and _ (max 32)" in result.payload["text"]

    async def test_name_at_max_length_accepted(self, dal: AsyncDB) -> None:
        max_name = "a" * 32
        result = await _run(f"!alias {max_name} echo hi")
        assert result is not None
        assert result.payload["text"] == f"alias set: !{max_name} → !echo hi"

    async def test_name_equal_to_bot_command_rejected(self) -> None:
        result = await _run("!alias ping echo hi")
        assert result is not None
        assert result.payload["text"] == "!ping is a built-in command and can't be aliased"

    async def test_name_equal_to_feature_command_rejected(self) -> None:
        result = await _run("!alias poll echo hi")
        assert result is not None
        assert result.payload["text"] == "!poll is a built-in command and can't be aliased"

    async def test_expansion_starting_with_alias_rejected(self) -> None:
        result = await _run("!alias newcmd alias list")
        assert result is not None
        assert result.payload["text"] == "an alias can't run !alias"

    async def test_expansion_starting_with_unalias_rejected(self) -> None:
        result = await _run("!alias newcmd unalias greet")
        assert result is not None
        assert result.payload["text"] == "an alias can't run !alias"

    async def test_expansion_unknown_command_rejected(self) -> None:
        result = await _run("!alias newcmd totallymadeupword foo")
        assert result is not None
        assert result.payload["text"] == "unknown command: totallymadeupword"

    async def test_expansion_flattens_existing_alias(self, dal: AsyncDB) -> None:
        result = await _run("!alias b greet extra")
        assert result is not None
        assert result.payload["text"] == "alias set: !b → !hello {user} {args} extra"

    async def test_expansion_flattens_existing_alias_no_trailing_args(self, dal: AsyncDB) -> None:
        """`!alias b greet` (no trailing args) flattens to `greet`'s own target verbatim."""
        result = await _run("!alias b greet")
        assert result is not None
        assert result.payload["text"] == "alias set: !b → !hello {user} {args}"

    async def test_expansion_too_long_rejected(self) -> None:
        long_expansion = "echo " + ("a" * 250)
        result = await _run(f"!alias newcmd {long_expansion}")
        assert result is not None
        assert "too long" in result.payload["text"]

    async def test_set_without_community_returns_guard(self, dal: AsyncDB) -> None:
        result = await _run("!alias newcmd echo hi", community=None)
        assert result is not None
        assert result.payload["text"] == _COMMUNITY_REQUIRED_MSG
        assert await _get_alias(dal, 2) is None

    async def test_set_handles_db_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Test error")

        monkeypatch.setattr(social_alias_process, "_upsert_alias", _raise)
        result = await _run("!alias newcmd echo hi")
        assert result is not None
        assert "Failed to set alias" in result.payload["text"]


class TestRemoveAlias:
    async def test_unalias_success(self, dal: AsyncDB, _invalidate_calls: list[Any]) -> None:
        result = await _run("!unalias greet")
        assert result is not None
        assert result.payload["text"] == "alias removed: !greet"
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is not None
        assert _invalidate_calls == [(1, "greet")]

    async def test_alias_delete_success(self, dal: AsyncDB) -> None:
        result = await _run("!alias delete greet")
        assert result is not None
        assert result.payload["text"] == "alias removed: !greet"
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is not None

    async def test_alias_remove_success(self, dal: AsyncDB) -> None:
        result = await _run("!alias remove greet")
        assert result is not None
        assert result.payload["text"] == "alias removed: !greet"
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is not None

    async def test_unalias_not_found(self) -> None:
        result = await _run("!unalias nosuchalias")
        assert result is not None
        assert result.payload["text"] == "no alias named !nosuchalias"

    async def test_unalias_missing_name_returns_usage(self) -> None:
        result = await _run("!unalias")
        assert result is not None
        assert result.payload["text"] == _ALIAS_USAGE

    async def test_alias_delete_missing_name_returns_usage(self) -> None:
        result = await _run("!alias delete")
        assert result is not None
        assert result.payload["text"] == _ALIAS_USAGE

    async def test_unalias_without_community_returns_guard(self, dal: AsyncDB) -> None:
        result = await _run("!unalias greet", community=None)
        assert result is not None
        assert result.payload["text"] == _COMMUNITY_REQUIRED_MSG
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is None

    async def test_unalias_handles_db_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Test error")

        monkeypatch.setattr(social_alias_process, "_soft_delete_alias", _raise)
        result = await _run("!unalias greet")
        assert result is not None
        assert "Failed to remove alias" in result.payload["text"]


class TestPermissions:
    async def test_set_denied_for_non_moderator(self, dal: AsyncDB) -> None:
        result = await _run("!alias newcmd echo hi", actor=NON_MOD_ACTOR)
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_MSG
        assert await _get_alias(dal, 2) is None

    async def test_unalias_denied_for_non_moderator(self, dal: AsyncDB) -> None:
        result = await _run("!unalias greet", actor=NON_MOD_ACTOR)
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_MSG
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is None

    async def test_set_denied_when_role_lookup_errors_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated permission lookup outage")

        monkeypatch.setattr("bundles.social_alias_process.raw_sql_rows", _raise)
        result = await _run("!alias newcmd echo hi")
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_MSG

    async def test_set_allowed_by_platform_user_id_match(self, dal: AsyncDB) -> None:
        await _seed_role_by_platform(dal, "plat-42", "admin")
        result = await _run("!alias newcmd echo hi", actor=NON_MOD_ACTOR, author_id="plat-42")
        assert result is not None
        assert result.payload["text"] == "alias set: !newcmd → !echo hi"

    async def test_list_does_not_require_permission(self) -> None:
        result = await _run("!alias list", actor=NON_MOD_ACTOR)
        assert result is not None
        assert result.payload["text"] != _PERMISSION_DENIED_MSG


class TestInvocation:
    async def test_bare_alias_invocation_returns_none_no_expansion(self, dal: AsyncDB) -> None:
        """Bare `!greet alice` returns None -- expansion is handled by bot_process, not here."""
        result = await _run("!greet alice")
        assert result is None
        # Verify no database lookup occurred
        assert dal.select_count == 0
        # Verify usage count didn't increment (no DB access)
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["usage_count"] == 5

    async def test_unknown_bang_word_returns_none_no_lookup(self, dal: AsyncDB) -> None:
        """Unknown bang-words like `!sr foo` return None with no DB lookup."""
        result = await _run("!sr foo")
        assert result is None
        assert dal.select_count == 0

    async def test_unknown_alias_returns_none(self) -> None:
        """Unknown alias `!notarealalias` returns None."""
        assert await _run("!notarealalias test") is None

    async def test_invocation_without_community_returns_none_no_query(self, dal: AsyncDB) -> None:
        """Bare invocation without community context returns None without query."""
        result = await _run("!greet alice", community=None)
        assert result is None
        assert dal.select_count == 0

    async def test_bare_invocation_no_db_access(self, dal: AsyncDB) -> None:
        """Bare invocation never touches the DB -- the early return happens before any query."""
        result = await _run("!greet alice")
        assert result is None
        assert dal.select_count == 0

    async def test_preserves_channel_id_on_response(self) -> None:
        result = await _run("!alias list", channel_id="chan-42")
        assert result is not None
        assert result.payload["channel_id"] == "chan-42"

    async def test_preserves_other_payload_fields(self) -> None:
        result = await _run("!alias list", extra="keep-me")
        assert result is not None
        assert result.payload["extra"] == "keep-me"

    async def test_original_event_not_mutated(self) -> None:
        event_ = _event("!alias list")
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(event_)
        assert result is not event_
        assert event_.payload["text"] == "!alias list"

    async def test_strips_leading_trailing_whitespace(self) -> None:
        result = await _run("  !alias list  ")
        assert result is not None
        assert result.payload["text"] != "  !alias list  "

    async def test_actor_none_defaults_gracefully(self) -> None:
        result = await _run("!alias list", actor=None)
        assert result is not None
        assert isinstance(result.payload["text"], str)


class TestCrossCommunityIsolation:
    """Regression: cross-community alias IDOR -- unchanged from prior behavior."""

    async def test_alias_not_listed_from_other_community(self) -> None:
        # regression: cross-community alias IDOR
        result = await _run("!alias list", community=COMMUNITY_2)
        assert result is not None
        assert "greet" not in result.payload["text"]
        assert result.payload["text"] == _NO_ALIASES_MSG

    async def test_alias_still_listed_from_its_own_community(self) -> None:
        # regression: cross-community alias IDOR
        result = await _run("!alias list", community=COMMUNITY)
        assert result is not None
        assert "greet" in result.payload["text"]

    async def test_alias_not_expanded_from_other_community(self) -> None:
        # regression: cross-community alias IDOR
        result = await _run("!greet penguin", community=COMMUNITY_2)
        assert result is None

    async def test_alias_not_deletable_from_other_community(self, dal: AsyncDB) -> None:
        # regression: cross-community alias IDOR
        result = await _run("!unalias greet", community=COMMUNITY_2)
        assert result is not None
        assert result.payload["text"] == "no alias named !greet"
        row = await _get_alias(dal, 1)
        assert row is not None
        assert row["deleted_at"] is None


class TestInvalidateAliasCacheGuard:
    async def test_missing_module_logs_debug_and_write_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`import services.command_alias_store` failing must never block a write."""

        def _raise_import_error(name: str) -> Any:
            raise ImportError(f"No module named {name!r}")

        monkeypatch.setattr(social_alias_process.importlib, "import_module", _raise_import_error)
        result = await _run("!alias newcmd echo hi")
        assert result is not None
        assert result.payload["text"] == "alias set: !newcmd → !echo hi"

    async def test_invalidate_failure_logs_debug_and_write_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(*, community_id: int, alias: str, redis_client: Any = None) -> None:
            raise RuntimeError("redis unreachable")

        monkeypatch.setattr(command_alias_store_module, "invalidate_alias", _boom)
        result = await _run("!alias newcmd echo hi")
        assert result is not None
        assert result.payload["text"] == "alias set: !newcmd → !echo hi"
