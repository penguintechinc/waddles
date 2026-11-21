"""`blueprints/v1/marketplace_catalog.py` -- unified catalog browsing, tenant-scoped.

Fail-first proof (executed, not narrated) for the cross-tenant-leak fix:
temporarily changed `services/marketplace_catalog_service.py::
_tenant_scope_query` to return `catalog.id > 0` (an unconditional
always-true query, i.e. Node's original unscoped behavior) instead of the
real tenant filter -- `test_anonymous_browse_excludes_other_tenants_
private_module` and `test_tenant_browse_excludes_other_tenants_private_
module` both went red (`other-private-widget` appeared in the response
for both an anonymous caller and a `TENANT_SLUG` caller); reverted,
green again. This is the same leak Node's `catalogService.js` has today
(no tenant filtering on the `marketplace_catalog` view at all) -- see
that module's own docstring.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_catalog import catalog_bp
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG


@pytest.fixture
def app(marketplace_catalog_db: Any) -> Quart:
    dal, _ids = marketplace_catalog_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(catalog_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def dal(marketplace_catalog_db: Any) -> Any:
    db, _ids = marketplace_catalog_db
    return db


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _names(payload: dict[str, Any]) -> set[str]:
    return {m["name"] for m in payload["modules"]}


class TestBrowseTenantScoping:
    async def test_anonymous_browse_excludes_other_tenants_private_module(
        self, client: Any
    ) -> None:
        response = await client.get("/api/v1/marketplace/catalog")
        assert response.status_code == 200
        payload = await response.get_json()
        names = _names(payload)
        assert names == {"core-widget", "global-vendor-widget"}
        assert "acme-private-widget" not in names
        assert "other-private-widget" not in names

    async def test_tenant_browse_includes_own_private_module(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.get(
            "/api/v1/marketplace/catalog", headers=auth_headers(tenant=TENANT_SLUG)
        )
        assert response.status_code == 200
        names = _names(await response.get_json())
        assert names == {"core-widget", "global-vendor-widget", "acme-private-widget"}

    async def test_tenant_browse_excludes_other_tenants_private_module(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.get(
            "/api/v1/marketplace/catalog", headers=auth_headers(tenant=TENANT_SLUG)
        )
        names = _names(await response.get_json())
        assert "other-private-widget" not in names

    async def test_other_tenant_sees_only_its_own_private_module(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.get(
            "/api/v1/marketplace/catalog", headers=auth_headers(tenant=OTHER_TENANT_SLUG)
        )
        names = _names(await response.get_json())
        assert names == {"core-widget", "global-vendor-widget", "other-private-widget"}
        assert "acme-private-widget" not in names

    async def test_invalid_token_falls_back_to_anonymous_scope(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/marketplace/catalog", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 200
        names = _names(await response.get_json())
        assert names == {"core-widget", "global-vendor-widget"}


class TestBrowseFiltersAndPagination:
    async def test_search_filters_by_name(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?search=core-widget")
        payload = await response.get_json()
        assert _names(payload) == {"core-widget"}

    async def test_source_filter(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?source=core")
        payload = await response.get_json()
        assert _names(payload) == {"core-widget"}

    async def test_pricing_type_filter(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?pricingType=free")
        payload = await response.get_json()
        assert _names(payload) == {"core-widget", "global-vendor-widget"}

    async def test_category_filter(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?category=utility")
        payload = await response.get_json()
        assert _names(payload) == {"core-widget", "global-vendor-widget"}

    async def test_pagination_shape(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?page=1&limit=1")
        payload = await response.get_json()
        assert len(payload["modules"]) == 1
        assert payload["pagination"] == {"page": 1, "limit": 1, "total": 2, "totalPages": 2}

    async def test_community_id_enriches_install_status(self, client: Any, dal: Any) -> None:
        community_id = dal.communities.insert(name="catalog-test-community")
        dal.hub_module_installations.insert(community_id=community_id, module_id=1, is_enabled=True)
        dal.marketplace_subscriptions.insert(
            community_id=community_id, module_id=1, is_enabled=False
        )
        dal.commit()

        response = await client.get(f"/api/v1/marketplace/catalog?communityId={community_id}")
        payload = await response.get_json()
        by_name = {m["name"]: m for m in payload["modules"]}
        assert by_name["core-widget"]["isInstalled"] is True
        assert by_name["core-widget"]["isEnabled"] is True
        assert by_name["global-vendor-widget"]["isInstalled"] is True
        assert by_name["global-vendor-widget"]["isEnabled"] is False

    async def test_avg_rating_is_a_string_like_node(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?source=core")
        payload = await response.get_json()
        assert payload["modules"][0]["avgRating"] == "4.5"

    async def test_non_numeric_page_and_limit_fall_back_to_defaults(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?page=notanumber&limit=notanumber")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["pagination"]["page"] == 1
        assert payload["pagination"]["limit"] == 25

    async def test_non_numeric_community_id_is_ignored(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog?communityId=notanumber")
        assert response.status_code == 200


class TestCategoriesAndFeatured:
    async def test_categories_response_shape(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/catalog/categories")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["success"] is True
        assert {c["category"] for c in payload["categories"]} == {"utility"}

    async def test_featured_is_tenant_scoped(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            "/api/v1/marketplace/catalog/featured", headers=auth_headers(tenant=TENANT_SLUG)
        )
        payload = await response.get_json()
        names = {m["name"] for m in payload["modules"]}
        assert "other-private-widget" not in names


def _uuid(dal: Any, table: str, numeric_id: int) -> str:
    """`public_id` of one seeded catalogue-backing row (the entry's public `sourceId`)."""
    row = dal(dal[table].id == numeric_id).select(dal[table].public_id).first()
    assert row is not None and row.public_id
    return str(row.public_id)


class TestCatalogEntry:
    async def test_get_own_tenant_entry(self, client: Any, auth_headers: Any, dal: Any) -> None:
        uuid_ = _uuid(dal, "marketplace_modules", 2)
        response = await client.get(
            f"/api/v1/marketplace/catalog/marketplace/{uuid_}",
            headers=auth_headers(tenant=TENANT_SLUG),
        )
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["module"]["name"] == "acme-private-widget"
        assert payload["module"]["sourceId"] == uuid_

    async def test_core_entry_resolves_against_hub_modules(self, client: Any, dal: Any) -> None:
        uuid_ = _uuid(dal, "hub_modules", 1)
        response = await client.get(f"/api/v1/marketplace/catalog/core/{uuid_}")
        assert response.status_code == 200
        assert (await response.get_json())["module"]["sourceId"] == uuid_

    async def test_get_other_tenants_entry_is_404(
        self, client: Any, auth_headers: Any, dal: Any
    ) -> None:
        """`other-private-widget` (tenant=OTHER_TENANT_SLUG) must 404 for a TENANT_SLUG caller."""
        uuid_ = _uuid(dal, "marketplace_modules", 3)
        response = await client.get(
            f"/api/v1/marketplace/catalog/marketplace/{uuid_}",
            headers=auth_headers(tenant=TENANT_SLUG),
        )
        assert response.status_code == 404
        payload = await response.get_json()
        assert payload["error"]["code"] == "NOT_FOUND"

    async def test_unknown_malformed_numeric_and_wrong_source_all_404(
        self, client: Any, dal: Any
    ) -> None:
        marketplace_uuid = _uuid(dal, "marketplace_modules", 1)
        for path in (
            "marketplace/00000000-0000-4000-8000-00000000dead",  # unknown UUID
            "marketplace/1",  # numeric id no longer routes
            "marketplace/not-a-uuid",
            f"core/{marketplace_uuid}",  # a marketplace UUID asked for as `core`
            f"bogus/{marketplace_uuid}",  # unknown source
        ):
            response = await client.get(f"/api/v1/marketplace/catalog/{path}")
            assert response.status_code == 404, path

    async def test_list_emits_uuid_source_ids_never_numeric(self, client: Any) -> None:
        payload = await (await client.get("/api/v1/marketplace/catalog")).get_json()
        source_ids = [m["sourceId"] for m in payload["modules"]]
        assert len(source_ids) == 2  # denominator: both visible rows were examined
        assert all(isinstance(v, str) and len(v) == 36 for v in source_ids)

    async def test_get_unknown_entry_is_404(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/marketplace/catalog/core/00000000-0000-4000-8000-000000009999"
        )
        assert response.status_code == 404
