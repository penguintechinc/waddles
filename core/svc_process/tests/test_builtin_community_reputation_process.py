"""Tests for `builtin_handlers.community_reputation_process` -- `!reputation`/`!rep`."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from flask_core.reputation_tiers import REPUTATION_TIERS as _SHARED_REPUTATION_TIERS
from flask_core.reputation_tiers import reputation_tier as _shared_reputation_tier
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

from builtin_handlers.community_reputation_process import _reputation_label, transform

#: This community row's `tenant_id` -- every test here operates under one
#: tenant; `TestTenantIsolation` seeds a second, different tenant_id to
#: prove the lookup never crosses it.
_TENANT_ID = 900


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
            sa_text(
                "CREATE TABLE communities (id INTEGER, display_name TEXT, name TEXT, "
                "tenant_id INTEGER)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE reputation_tenant (tenant_id INTEGER, hub_user_id TEXT, "
                "score INTEGER)"
            )
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


async def _insert_community(
    conn: object, *, community_id: int, display_name: str, name: str, tenant_id: int
) -> None:
    """Bound-parameter INSERT -- avoids building SQL by string interpolation (S608)."""
    await conn.execute(  # type: ignore[attr-defined]
        sa_text(
            "INSERT INTO communities (id, display_name, name, tenant_id) "
            "VALUES (:id, :display_name, :name, :tenant_id)"
        ),
        {"id": community_id, "display_name": display_name, "name": name, "tenant_id": tenant_id},
    )


async def _insert_tenant_score(
    conn: object, *, tenant_id: int, hub_user_id: str, score: int
) -> None:
    """Bound-parameter INSERT -- avoids building SQL by string interpolation (S608)."""
    await conn.execute(  # type: ignore[attr-defined]
        sa_text(
            "INSERT INTO reputation_tenant (tenant_id, hub_user_id, score) "
            "VALUES (:tenant_id, :hub_user_id, :score)"
        ),
        {"tenant_id": tenant_id, "hub_user_id": hub_user_id, "score": score},
    )


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
    async def test_reply_shows_both_tenant_and_community_with_labels(self, dal: AsyncDB) -> None:
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_members "
                    "(community_id, platform, platform_user_id, display_name, reputation, user_id) "
                    "VALUES (4, 'twitch', 'u-123', 'penguinzplays', 720, '42')"
                )
            )
            await _insert_community(
                conn,
                community_id=4,
                display_name="Waddlebot HQ",
                name="waddlebot_hq",
                tenant_id=_TENANT_ID,
            )
            await _insert_tenant_score(conn, tenant_id=_TENANT_ID, hub_user_id="42", score=600)
        result = await _run(dal, "!reputation", author_id="u-123")
        assert result is not None
        assert result.payload["text"] == (
            "\U0001f427 penguinzplays — Tenant: 600 (Trusted) · " "Waddlebot HQ: 720 (Respected)"
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
            await _insert_community(
                conn,
                community_id=4,
                display_name="Waddlebot HQ",
                name="waddlebot_hq",
                tenant_id=_TENANT_ID,
            )
        result = await _run(dal, "!rep")
        assert result is not None
        assert "Waddlebot HQ: 655 (Trusted)" in result.payload["text"]
        assert "Tenant: 600 (Trusted)" in result.payload["text"]

    async def test_new_user_defaults_both_sides_to_600(self, dal: AsyncDB) -> None:
        """No `community_members` row and no `reputation_tenant` row -> 600 (Trusted) both sides."""
        async with dal.engine.begin() as conn:
            await _insert_community(
                conn,
                community_id=4,
                display_name="Waddlebot HQ",
                name="waddlebot_hq",
                tenant_id=_TENANT_ID,
            )
        result = await _run(dal, "!reputation", actor="stranger")
        assert result is not None
        assert result.payload["text"] == (
            "\U0001f427 stranger — Tenant: 600 (Trusted) · Waddlebot HQ: 600 (Trusted)"
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


class TestTenantIsolation:
    async def test_tenant_score_never_leaks_from_another_tenants_community(
        self, dal: AsyncDB
    ) -> None:
        """A `reputation_tenant` row seeded under a DIFFERENT tenant_id must never surface here.

        security.md Tenant Isolation: tenant is resolved from `community_id`
        (community 4 -> tenant `_TENANT_ID`) via `_TENANT_SCORE_SQL`'s join
        -- a row for the SAME `hub_user_id` under a different tenant_id
        (seeded here under community 5 -> tenant `_TENANT_ID + 1`) must
        never be returned for community 4's lookup.
        """
        other_tenant_id = _TENANT_ID + 1
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_members "
                    "(community_id, platform, platform_user_id, display_name, reputation, user_id) "
                    "VALUES (4, 'twitch', 'u-123', 'penguinzplays', 720, '42')"
                )
            )
            await _insert_community(
                conn,
                community_id=4,
                display_name="Waddlebot HQ",
                name="waddlebot_hq",
                tenant_id=_TENANT_ID,
            )
            # A second community belonging to a DIFFERENT tenant, plus a
            # reputation_tenant row for the SAME hub_user_id under that
            # other tenant -- the exact shape a tenant-forgetting query
            # would conflate.
            await _insert_community(
                conn,
                community_id=5,
                display_name="Other Org HQ",
                name="other_org_hq",
                tenant_id=other_tenant_id,
            )
            await _insert_tenant_score(conn, tenant_id=other_tenant_id, hub_user_id="42", score=850)
        result = await _run(dal, "!reputation", author_id="u-123")
        assert result is not None
        # Community 4's tenant has NO reputation_tenant row of its own for
        # hub_user_id=42 -- baseline 600, never the other tenant's 850.
        assert "Tenant: 600 (Trusted)" in result.payload["text"]
        assert "850" not in result.payload["text"]


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

    def test_label_fn_uses_the_shared_flask_core_tier_table(self, dal: AsyncDB) -> None:
        """`_reputation_label` IS `flask_core.reputation_tiers.reputation_tier` -- no local copy.

        Previously this bundle and `hub_api/services/
        community_reputation_service.py` each kept a hand-mirrored
        `REPUTATION_TIERS` constant, guarded only by a source-parsing drift
        test (parsing hub-api's file as AST, since a runtime cross-import
        between the two independently-deployed processes' own top-level
        packages would resolve to the wrong one). Both processes already
        depend on `flask_core` -- importing the tier table from there
        instead eliminates the duplicate copies this test used to guard,
        rather than needing to keep guarding them.
        """
        from builtin_handlers import community_reputation_process

        assert community_reputation_process._reputation_label is _shared_reputation_tier
        for score, expected in [(300, "Newcomer"), (850, "Legend")]:
            assert _reputation_label(score) == expected
        assert _SHARED_REPUTATION_TIERS[0] == (465, "Newcomer")
