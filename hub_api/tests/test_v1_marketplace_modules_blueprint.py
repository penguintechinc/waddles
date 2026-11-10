"""`blueprints/v1/marketplace_modules.py` -- `hub_modules` CRUD, port of `moduleController.js`.

Fail-first proof (executed, not narrated) for the auth-bypass regression
test: temporarily removed the `@require_scope(_ADMIN_SCOPE)` decorator
from `create_module_route` -- `test_create_without_token_is_401` (a
request with NO `Authorization` header) went red (201 instead of 401,
an auth bypass on module creation); reverted, green again. Separately,
temporarily changed `_ADMIN_SCOPE` to an empty string --
`test_create_with_wrong_scope_is_403` went red (201 instead of 403,
since an empty-string required scope trivially matches); reverted,
green again.
"""

from __future__ import annotations

import json as json_module
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_modules import modules_bp

_ADMIN_SCOPE = "marketplace.modules:admin"


@pytest.fixture
def app(marketplace_catalog_db: Any) -> Quart:
    dal, _ids = marketplace_catalog_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(modules_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def ids(marketplace_catalog_db: Any) -> dict[str, int]:
    _dal, seeded_ids = marketplace_catalog_db
    return seeded_ids


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


async def _post_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.post(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


async def _put_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.put(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


class TestPublicBrowse:
    async def test_browse_only_shows_published(self, client: Any) -> None:
        response = await client.get("/api/v1/modules")
        assert response.status_code == 200
        payload = await response.get_json()
        names = {m["name"] for m in payload["modules"]}
        assert "published-module" in names
        assert "core-module" in names
        assert "unpublished-module" not in names

    async def test_browse_search_filter(self, client: Any) -> None:
        response = await client.get("/api/v1/modules?search=published-module")
        payload = await response.get_json()
        assert {m["name"] for m in payload["modules"]} == {"published-module"}

    async def test_browse_featured_filter(self, client: Any) -> None:
        response = await client.get("/api/v1/modules?featured=true")
        payload = await response.get_json()
        assert {m["name"] for m in payload["modules"]} == {"published-module"}

    async def test_browse_category_filter(self, client: Any) -> None:
        response = await client.get("/api/v1/modules?category=utility")
        payload = await response.get_json()
        assert {m["name"] for m in payload["modules"]} == {"published-module"}

    async def test_browse_non_numeric_page_and_limit_fall_back_to_defaults(
        self, client: Any
    ) -> None:
        response = await client.get("/api/v1/modules?page=notanumber&limit=notanumber")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["pagination"]["page"] == 1
        assert payload["pagination"]["limit"] == 25

    async def test_module_details_includes_reviews(self, client: Any, ids: dict[str, int]) -> None:
        response = await client.get(f"/api/v1/modules/{ids['published_module_id']}")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["module"]["name"] == "published-module"
        assert payload["module"]["reviewCount"] == 1
        assert payload["module"]["avgRating"] == "5.0"
        assert len(payload["module"]["reviews"]) == 1

    async def test_unpublished_module_details_is_404(
        self, client: Any, ids: dict[str, int]
    ) -> None:
        response = await client.get(f"/api/v1/modules/{ids['unpublished_module_id']}")
        assert response.status_code == 404

    async def test_unknown_module_details_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/modules/999999")
        assert response.status_code == 404


class TestAuthBypassAndScope:
    async def test_create_without_token_is_401(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/modules",
            headers={"Content-Type": "application/json"},
            data=json_module.dumps({"name": "new-module", "version": "1.0.0"}),
        )
        assert response.status_code == 401

    async def test_create_with_wrong_scope_is_403(self, client: Any, auth_headers: Any) -> None:
        response = await _post_json(
            client,
            "/api/v1/modules",
            auth_headers(scope="marketplace.catalog:read"),
            {"name": "new-module", "version": "1.0.0"},
        )
        assert response.status_code == 403

    async def test_update_without_token_is_401(self, client: Any, ids: dict[str, int]) -> None:
        response = await client.put(
            f"/api/v1/modules/{ids['published_module_id']}",
            headers={"Content-Type": "application/json"},
            data=json_module.dumps({"description": "new description"}),
        )
        assert response.status_code == 401

    async def test_delete_without_token_is_401(self, client: Any, ids: dict[str, int]) -> None:
        response = await client.delete(f"/api/v1/modules/{ids['published_module_id']}")
        assert response.status_code == 401

    async def test_subscriptions_without_token_is_401(
        self, client: Any, ids: dict[str, int]
    ) -> None:
        response = await client.get(f"/api/v1/modules/{ids['published_module_id']}/subscriptions")
        assert response.status_code == 401


class TestCreateUpdateDelete:
    async def test_create_requires_name_and_version(self, client: Any, auth_headers: Any) -> None:
        response = await _post_json(
            client, "/api/v1/modules", auth_headers(scope=_ADMIN_SCOPE, user_id="1"), {}
        )
        assert response.status_code == 400

    async def test_create_rejects_bad_icon_url(self, client: Any, auth_headers: Any) -> None:
        response = await _post_json(
            client,
            "/api/v1/modules",
            auth_headers(scope=_ADMIN_SCOPE, user_id="1"),
            {"name": "widget", "version": "1.0.0", "iconUrl": "not-a-url"},
        )
        assert response.status_code == 400

    async def test_full_create_update_delete_flow(self, client: Any, auth_headers: Any) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")

        create_resp = await _post_json(
            client,
            "/api/v1/modules",
            headers,
            {"name": "brand-new-widget", "version": "1.0.0", "displayName": "Brand New Widget"},
        )
        assert create_resp.status_code == 201
        new_id = (await create_resp.get_json())["module"]["id"]

        # Newly-created modules are unpublished (matches Node) -- not visible
        # via the public browse/detail routes until published via update.
        detail_resp = await client.get(f"/api/v1/modules/{new_id}")
        assert detail_resp.status_code == 404

        update_resp = await _put_json(
            client, f"/api/v1/modules/{new_id}", headers, {"isPublished": True}
        )
        assert update_resp.status_code == 200

        published_resp = await client.get(f"/api/v1/modules/{new_id}")
        assert published_resp.status_code == 200

        delete_resp = await client.delete(f"/api/v1/modules/{new_id}", headers=headers)
        assert delete_resp.status_code == 200

        gone_resp = await client.get(f"/api/v1/modules/{new_id}")
        assert gone_resp.status_code == 404

    async def test_create_duplicate_name_is_409(self, client: Any, auth_headers: Any) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        first = await _post_json(
            client, "/api/v1/modules", headers, {"name": "dupe-widget", "version": "1.0.0"}
        )
        assert first.status_code == 201
        second = await _post_json(
            client, "/api/v1/modules", headers, {"name": "dupe-widget", "version": "1.0.0"}
        )
        assert second.status_code == 409

    async def test_create_normalizes_github_author_and_detail_links_it(
        self, client: Any, auth_headers: Any
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        created = await _post_json(
            client,
            "/api/v1/modules",
            headers,
            {"name": "gh-widget", "version": "1.0.0", "author": "https://github.com/octo-cat/"},
        )
        assert created.status_code == 201
        new_id = (await created.get_json())["module"]["id"]
        await _put_json(client, f"/api/v1/modules/{new_id}", headers, {"isPublished": True})
        detail = (await (await client.get(f"/api/v1/modules/{new_id}")).get_json())["module"]
        assert detail["author"] == {
            "display": "octo-cat",
            "githubHandle": "octo-cat",
            "profileUrl": "https://github.com/octo-cat",
        }

    @pytest.mark.parametrize("author", ["<script>x</script>", "https://evil.example/a", 5])
    async def test_create_rejects_invalid_author(
        self, client: Any, auth_headers: Any, author: Any
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await _post_json(
            client,
            "/api/v1/modules",
            headers,
            {"name": "bad-author-widget", "version": "1.0.0", "author": author},
        )
        assert response.status_code == 400

    async def test_update_rejects_invalid_author(
        self, client: Any, auth_headers: Any, ids: dict[str, int]
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await _put_json(
            client,
            f"/api/v1/modules/{ids['published_module_id']}",
            headers,
            {"author": "@-bad"},
        )
        assert response.status_code == 400

    async def test_delete_core_module_is_404(
        self, client: Any, auth_headers: Any, ids: dict[str, int]
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await client.delete(f"/api/v1/modules/{ids['core_module_id']}", headers=headers)
        assert response.status_code == 404

    async def test_update_no_fields_is_400(
        self, client: Any, auth_headers: Any, ids: dict[str, int]
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await _put_json(
            client, f"/api/v1/modules/{ids['published_module_id']}", headers, {}
        )
        assert response.status_code == 400

    async def test_update_every_field(
        self, client: Any, auth_headers: Any, ids: dict[str, int]
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await _put_json(
            client,
            f"/api/v1/modules/{ids['published_module_id']}",
            headers,
            {
                "displayName": "Updated Display Name",
                "description": "Updated description",
                "version": "2.0.0",
                "author": "New Author",
                "category": "new-category",
                "iconUrl": "https://example.com/new-icon.png",
                "isFeatured": False,
                "isPublished": True,
            },
        )
        assert response.status_code == 200

        detail_resp = await client.get(f"/api/v1/modules/{ids['published_module_id']}")
        detail = (await detail_resp.get_json())["module"]
        assert detail["displayName"] == "Updated Display Name"
        assert detail["description"] == "Updated description"
        assert detail["version"] == "2.0.0"
        assert detail["author"] == {
            "display": "New Author",
            "githubHandle": None,
            "profileUrl": None,
        }
        assert detail["category"] == "new-category"
        assert detail["iconUrl"] == "https://example.com/new-icon.png"
        assert detail["isFeatured"] is False

    async def test_update_unknown_module_is_404(self, client: Any, auth_headers: Any) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await _put_json(client, "/api/v1/modules/999999", headers, {"description": "x"})
        assert response.status_code == 404


class TestSubscriptions:
    async def test_subscriptions_lists_installations(
        self, client: Any, auth_headers: Any, ids: dict[str, int]
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await client.get(
            f"/api/v1/modules/{ids['published_module_id']}/subscriptions", headers=headers
        )
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["total"] == 1
        sub = payload["subscriptions"][0]
        assert sub["communityId"] == ids["community_id"]
        assert sub["communityLogo"] == "https://example.com/logo.png"
        assert sub["isEnabled"] is True

    async def test_subscriptions_empty_for_uninstalled_module(
        self, client: Any, auth_headers: Any, ids: dict[str, int]
    ) -> None:
        headers = auth_headers(scope=_ADMIN_SCOPE, user_id="1")
        response = await client.get(
            f"/api/v1/modules/{ids['core_module_id']}/subscriptions", headers=headers
        )
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["total"] == 0
        assert payload["subscriptions"] == []
