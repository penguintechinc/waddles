"""Tests for `services.community_context_store` -- the `!cc` handler's DB+Redis-backed store.

`dal`/`redis_client` are always passed explicitly (this module's own
test-injection override params) except for the small set of
`TestDefaultResolution` cases that exercise the real
`flask_core.get_bundle_dal()`/singleton-Redis-client fallback paths the
`!cc` handler itself relies on (it calls every function with ONLY the
documented required keyword arguments).
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.bundle_runtime import reset_bundle_dal_for_tests, set_bundle_dal

from services import community_context_store as store_module
from services.community_context_store import (
    DEFAULT_CONTEXT_TTL_S,
    ChannelCommunity,
    _context_cache_key,
    clear_context,
    get_context,
    list_channel_communities,
    reset_redis_client_for_tests,
    set_context,
)

PLATFORM = "discord"
ENTITY_ID = "chan-1"
USER_ID = "u-1"
COMMUNITY_ID = 42


class _FakeDal:
    """Records every `execute()` call; returns a preloaded row set for that SQL."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows if rows is not None else []
        self.calls: list[tuple[str, list[Any] | None]] = []

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        self.calls.append((sql, params))
        return self._rows


@pytest.fixture(autouse=True)
def _isolate_singletons() -> Any:
    """Reset both module-level singletons around every test -- no cross-test leakage."""
    reset_bundle_dal_for_tests()
    reset_redis_client_for_tests()
    yield
    reset_bundle_dal_for_tests()
    reset_redis_client_for_tests()


class TestContextCacheKey:
    def test_entity_before_user_in_key(self) -> None:
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        assert key == f"ctx:{PLATFORM}:{ENTITY_ID}:{USER_ID}"


class TestListChannelCommunities:
    async def test_returns_ordered_channel_communities(self) -> None:
        dal = _FakeDal(
            rows=[
                {"id": 4, "name": "waddlebot", "is_primary": True},
                {"id": 9, "name": "alt-community", "is_primary": False},
            ]
        )
        result = await list_channel_communities(
            platform=PLATFORM, platform_entity_id=ENTITY_ID, dal=dal
        )
        assert result == [
            ChannelCommunity(id=4, name="waddlebot", is_primary=True),
            ChannelCommunity(id=9, name="alt-community", is_primary=False),
        ]
        assert dal.calls[0][1] == [PLATFORM, ENTITY_ID]

    async def test_no_approved_links_returns_empty_list(self) -> None:
        dal = _FakeDal(rows=[])
        result = await list_channel_communities(
            platform=PLATFORM, platform_entity_id=ENTITY_ID, dal=dal
        )
        assert result == []


class TestGetContext:
    async def test_redis_hit_never_queries_db(self, redis_client: Any) -> None:
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        await redis_client.set(key, str(COMMUNITY_ID))
        dal = _FakeDal(rows=[{"community_id": 999}])  # would be wrong if DB were queried

        result = await get_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            dal=dal,
            redis_client=redis_client,
        )

        assert result == COMMUNITY_ID
        assert dal.calls == []

    async def test_redis_miss_falls_back_to_table_and_rewarms_cache(
        self, redis_client: Any
    ) -> None:
        dal = _FakeDal(rows=[{"community_id": COMMUNITY_ID}])

        result = await get_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            dal=dal,
            redis_client=redis_client,
        )

        assert result == COMMUNITY_ID
        assert dal.calls[0][1] == [PLATFORM, USER_ID, ENTITY_ID]
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        assert await redis_client.get(key) == str(COMMUNITY_ID)
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= DEFAULT_CONTEXT_TTL_S

    async def test_expired_cache_key_behaves_like_a_miss(self, redis_client: Any) -> None:
        """A TTL-expired Redis key GETs as `None`, identical to never having been cached."""
        dal = _FakeDal(rows=[{"community_id": COMMUNITY_ID}])

        result = await get_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            dal=dal,
            redis_client=redis_client,
        )

        assert result == COMMUNITY_ID
        assert len(dal.calls) == 1  # DB was consulted, exactly like any other miss

    async def test_no_override_anywhere_returns_none(self, redis_client: Any) -> None:
        dal = _FakeDal(rows=[])

        result = await get_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            dal=dal,
            redis_client=redis_client,
        )

        assert result is None
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        assert await redis_client.get(key) is None


class TestSetContext:
    async def test_upserts_table_and_sets_cache_with_default_ttl(self, redis_client: Any) -> None:
        dal = _FakeDal(rows=[])

        await set_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            community_id=COMMUNITY_ID,
            dal=dal,
            redis_client=redis_client,
        )

        assert dal.calls[0][1] == [PLATFORM, USER_ID, ENTITY_ID, COMMUNITY_ID]
        assert "INSERT INTO user_platform_context" in dal.calls[0][0]
        assert "ON CONFLICT" in dal.calls[0][0]
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        assert await redis_client.get(key) == str(COMMUNITY_ID)
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= DEFAULT_CONTEXT_TTL_S

    async def test_honors_custom_ttl(self, redis_client: Any) -> None:
        dal = _FakeDal(rows=[])

        await set_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            community_id=COMMUNITY_ID,
            ttl_s=60,
            dal=dal,
            redis_client=redis_client,
        )

        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= 60


class TestClearContext:
    async def test_deletes_table_row_and_cache_key(self, redis_client: Any) -> None:
        dal = _FakeDal(rows=[])
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        await redis_client.set(key, str(COMMUNITY_ID))

        await clear_context(
            platform=PLATFORM,
            platform_user_id=USER_ID,
            platform_entity_id=ENTITY_ID,
            dal=dal,
            redis_client=redis_client,
        )

        assert dal.calls[0][1] == [PLATFORM, USER_ID, ENTITY_ID]
        assert "DELETE FROM user_platform_context" in dal.calls[0][0]
        assert await redis_client.get(key) is None


class TestDefaultResolution:
    """No `dal`/`redis_client` override -- the real `!cc` bundle calling convention."""

    async def test_list_channel_communities_uses_bound_dal_by_default(self) -> None:
        dal = _FakeDal(rows=[{"id": 1, "name": "waddlebot", "is_primary": True}])
        set_bundle_dal(dal)

        result = await list_channel_communities(platform=PLATFORM, platform_entity_id=ENTITY_ID)

        assert result == [ChannelCommunity(id=1, name="waddlebot", is_primary=True)]

    async def test_get_context_uses_singleton_redis_client_by_default(
        self, redis_client: Any
    ) -> None:
        # Pre-seed the module's lazy singleton with the fake client -- avoids a
        # real network attempt against `Config.VALKEY_URL` while still exercising
        # the exact "no redis_client kwarg supplied" path the `!cc` bundle uses.
        store_module._redis_client = redis_client
        dal = _FakeDal(rows=[])
        set_bundle_dal(dal)
        key = _context_cache_key(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )
        await redis_client.set(key, str(COMMUNITY_ID))

        result = await get_context(
            platform=PLATFORM, platform_user_id=USER_ID, platform_entity_id=ENTITY_ID
        )

        assert result == COMMUNITY_ID
        assert dal.calls == []
