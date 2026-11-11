"""`blueprints/v1/public.py` -- the M3 Public group (publicController.js).

PRE-AUTH: no `auth_headers`/`user_auth_headers` fixtures used anywhere in
this file -- every request is bare, matching `hub_api/PORTING.md`'s Auth
pattern table.

Fail-first proof (executed, not narrated): temporarily removed the
`tenant_scoped(...)` wrapper from `public_service.list_communities()`'s
query (left the bare `is_active & is_public` filter) -- `test_list_
communities_excludes_other_tenant` went red (a second tenant's public
community leaked into the response, the exact cross-tenant leak this
group's SECURITY fix closes); reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.public import public_bp, signup_settings_alias_bp
from config import HubAPIConfig
from services import landing_stats_cache
from tests.conftest import TENANT_SLUG


class FakeValkey:
    """Minimal in-memory stand-in for the redis.asyncio client (get/set with TTL record)."""

    def __init__(self, fail: bool = False) -> None:
        """Start empty; `fail=True` makes every get raise."""
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("valkey down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug=TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(platform_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(public_bp)
    quart_app.register_blueprint(signup_settings_alias_bp)
    quart_app.config["dal"] = platform_db.dal
    quart_app.config["async_dal"] = platform_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_community(
    platform_db: Any,
    *,
    tenant_id: int,
    name: str = "acme",
    is_active: bool = True,
    is_public: bool = True,
    community_type: str | None = "creator",
    member_count: int = 5,
) -> int:
    row_id: int = platform_db.dal.communities.insert(
        name=name,
        display_name=name.title(),
        is_active=is_active,
        is_public=is_public,
        community_type=community_type,
        member_count=member_count,
        tenant_id=tenant_id,
        created_at=datetime.now(UTC),
    )
    platform_db.dal.commit()
    return row_id


class TestStatsAndBanner:
    async def test_get_stats_serves_cached_snapshot(self, app: Quart, client: Any) -> None:
        fake = FakeValkey()
        app.config[landing_stats_cache.CLIENT_CONFIG_KEY] = fake
        stats = {
            "communities": 2,
            "discord": {"servers": 1, "channels": 1},
            "twitch": {"channels": 1, "live": 1, "viewers": 10},
            "slack": {"workspaces": 1, "channels": 1},
        }
        snap = await landing_stats_cache.write_snapshot(fake, stats)
        response = await client.get("/api/v1/public/stats")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stats"] == stats
        assert body["computedAt"] == snap["computed_at"]
        assert fake.ttls[landing_stats_cache.SNAPSHOT_KEY] == 1200

    async def test_get_stats_miss_is_warming_503_without_db(
        self, app: Quart, client: Any, platform_db: Any, caplog: Any
    ) -> None:
        app.config[landing_stats_cache.CLIENT_CONFIG_KEY] = FakeValkey()
        # Default tenant inactive: an on-request DB fallback would 500; warming proves none runs.
        platform_db.dal(platform_db.dal.tenants.slug == TENANT_SLUG).update(is_active=False)
        platform_db.dal.commit()
        response = await client.get("/api/v1/public/stats")
        assert response.status_code == 503
        assert await response.get_json() == {"status": "warming"}
        assert "snapshot missing" in caplog.text

    async def test_get_stats_valkey_error_is_warming_503(self, app: Quart, client: Any) -> None:
        app.config[landing_stats_cache.CLIENT_CONFIG_KEY] = FakeValkey(fail=True)
        response = await client.get("/api/v1/public/stats")
        assert response.status_code == 503
        assert await response.get_json() == {"status": "warming"}

    async def test_corrupt_snapshot_is_miss(self) -> None:
        fake = FakeValkey()
        fake.store[landing_stats_cache.SNAPSHOT_KEY] = "not json"
        assert await landing_stats_cache.read_snapshot(fake) is None

    async def test_get_banner_has_no_success_envelope(self, client: Any) -> None:
        response = await client.get("/api/v1/public/banner")
        assert response.status_code == 200
        body = await response.get_json()
        assert "success" not in body
        assert body["enabled"] is False
        assert body["bgColor"] == "#F5C518"


class TestCommunities:
    async def test_list_communities_missing_default_tenant_is_500(
        self, client: Any, platform_db: Any
    ) -> None:
        platform_db.dal(platform_db.dal.tenants.slug == TENANT_SLUG).update(is_active=False)
        platform_db.dal.commit()
        response = await client.get("/api/v1/public/communities")
        assert response.status_code == 500

    async def test_spotlighted_missing_default_tenant_is_500(
        self, client: Any, platform_db: Any
    ) -> None:
        platform_db.dal(platform_db.dal.tenants.slug == TENANT_SLUG).update(is_active=False)
        platform_db.dal.commit()
        response = await client.get("/api/v1/public/communities/spotlighted")
        assert response.status_code == 500

    async def test_list_communities_excludes_inactive_and_private(
        self, client: Any, platform_db: Any
    ) -> None:
        _seed_community(platform_db, tenant_id=1, name="visible", is_active=True, is_public=True)
        _seed_community(platform_db, tenant_id=1, name="inactive", is_active=False)
        _seed_community(platform_db, tenant_id=1, name="private", is_public=False)

        response = await client.get("/api/v1/public/communities")
        assert response.status_code == 200
        body = await response.get_json()
        names = [c["name"] for c in body["communities"]]
        assert names == ["visible"]

    async def test_list_communities_excludes_other_tenant(
        self, client: Any, platform_db: Any
    ) -> None:
        """The regression proof for this group's SECURITY fix (cross-tenant leak)."""
        platform_db.dal.tenants.insert(slug="other-tenant", is_active=True)
        platform_db.dal.commit()
        other_tenant_id = (
            platform_db.dal(platform_db.dal.tenants.slug == "other-tenant").select().first().id
        )

        _seed_community(platform_db, tenant_id=1, name="own-tenant-community")
        _seed_community(platform_db, tenant_id=other_tenant_id, name="other-tenant-community")

        response = await client.get("/api/v1/public/communities")
        body = await response.get_json()
        names = [c["name"] for c in body["communities"]]
        assert names == ["own-tenant-community"]

    async def test_get_community_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/public/communities/9999")
        assert response.status_code == 404

    async def test_get_community_found(self, client: Any, platform_db: Any) -> None:
        community_id = _seed_community(platform_db, tenant_id=1)
        response = await client.get(f"/api/v1/public/communities/{community_id}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["community"]["id"] == community_id

    async def test_spotlighted_excludes_support_type(
        self, client: Any, platform_db: Any
    ) -> None:
        _seed_community(platform_db, tenant_id=1, name="creator-a", community_type="creator")
        _seed_community(platform_db, tenant_id=1, name="support-team", community_type="support")

        response = await client.get("/api/v1/public/communities/spotlighted")
        body = await response.get_json()
        names = [c["name"] for c in body["communities"]]
        assert "creator-a" in names
        assert "support-team" not in names


class TestLiveStreams:
    async def test_get_live_streams_empty(self, client: Any) -> None:
        response = await client.get("/api/v1/public/live")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["streams"] == []

    async def test_get_stream_details_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/public/live/does-not-exist")
        assert response.status_code == 404

    async def test_get_stream_details_found(self, client: Any, platform_db: Any) -> None:
        platform_db.dal.coordination.insert(
            entity_id="ent-1", platform="twitch", is_live=True, viewer_count=42
        )
        platform_db.dal.commit()
        response = await client.get("/api/v1/public/live/ent-1")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stream"]["viewerCount"] == 42
        assert body["stream"]["isLive"] is True


class TestSignupSettings:
    async def test_defaults_signup_disabled_without_email(self, client: Any) -> None:
        response = await client.get("/api/v1/public/signup-settings")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["signupEnabled"] is False

    async def test_top_level_alias_matches_prefixed_route(self, client: Any) -> None:
        response = await client.get("/api/v1/signup-settings")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True


class TestMarketplace:
    async def test_get_marketplace_modules_only_published(
        self, client: Any, platform_db: Any
    ) -> None:
        platform_db.dal.hub_modules.insert(
            name="published-mod", is_published=True, created_at=datetime.now(UTC)
        )
        platform_db.dal.hub_modules.insert(
            name="draft-mod", is_published=False, created_at=datetime.now(UTC)
        )
        platform_db.dal.commit()

        response = await client.get("/api/v1/public/marketplace/modules")
        assert response.status_code == 200
        body = await response.get_json()
        names = [m["name"] for m in body["modules"]]
        assert names == ["published-mod"]
        assert body["modules"][0]["avgRating"] == "0.0"

    async def test_get_marketplace_modules_search_and_category_filters(
        self, client: Any, platform_db: Any
    ) -> None:
        platform_db.dal.hub_modules.insert(
            name="chat-bot",
            display_name="Chat Bot",
            category="chat",
            is_published=True,
            created_at=datetime.now(UTC),
        )
        platform_db.dal.hub_modules.insert(
            name="poll-tool",
            display_name="Poll Tool",
            category="games",
            is_published=True,
            created_at=datetime.now(UTC),
        )
        platform_db.dal.commit()

        by_search = await client.get(
            "/api/v1/public/marketplace/modules", query_string={"search": "chat"}
        )
        body = await by_search.get_json()
        assert [m["name"] for m in body["modules"]] == ["chat-bot"]

        by_category = await client.get(
            "/api/v1/public/marketplace/modules", query_string={"category": "games"}
        )
        body2 = await by_category.get_json()
        assert [m["name"] for m in body2["modules"]] == ["poll-tool"]

    async def test_get_marketplace_module_not_found_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/public/marketplace/modules/9999")
        assert response.status_code == 404

    async def test_get_marketplace_module_with_reviews(
        self, client: Any, platform_db: Any
    ) -> None:
        module_id = platform_db.dal.hub_modules.insert(
            name="reviewed-mod", is_published=True, created_at=datetime.now(UTC)
        )
        platform_db.dal.hub_module_reviews.insert(
            module_id=module_id, rating=5, review_text="Great!", created_at=datetime.now(UTC)
        )
        platform_db.dal.commit()

        response = await client.get(f"/api/v1/public/marketplace/modules/{module_id}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["module"]["avgRating"] == "5.0"
        assert body["module"]["reviews"][0]["author"] == "Anonymous"

    async def test_get_marketplace_categories(self, client: Any, platform_db: Any) -> None:
        platform_db.dal.hub_modules.insert(
            name="mod-a", is_published=True, category="games", created_at=datetime.now(UTC)
        )
        platform_db.dal.hub_modules.insert(
            name="mod-b", is_published=True, category="games", created_at=datetime.now(UTC)
        )
        platform_db.dal.commit()

        response = await client.get("/api/v1/public/marketplace/categories")
        body = await response.get_json()
        assert body["categories"] == [{"name": "games", "moduleCount": 2}]
