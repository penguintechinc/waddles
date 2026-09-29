"""`blueprints/v1/marketplace_lifecycle.py` -- App Bundle 3-tier lifecycle group.

Standalone Quart app registering only `marketplace_lifecycle_bp`, matching
`test_v1_tenant_blueprint.py`'s pattern.

Fail-first proofs (executed, not narrated):

1. Superset invariant #1 (`available <= installed`) -- temporarily replaced
   `services/marketplace_lifecycle_service.py::make_available`'s
   `check_availability_insert_allowed` call with a bare `pass`:
   `test_make_available_without_install_is_409` went red (201 instead of
   409, a tenant could make ANY app_id "available" with no corresponding
   `app_catalog` row); reverted, green again.
2. Superset invariant #2 (`activated <= available`) -- temporarily replaced
   `activate_bundle`'s `check_activation_insert_allowed` call with a bare
   `pass`: `test_activate_without_availability_is_409` went red (201
   instead of 409); reverted, green again.
3. Per-community IDOR (`resolve_community_membership_scoped`'s
   tenant-ownership guard, `services/community_authz.py`) -- temporarily
   changed its `if not community_rows:` early-return to `if False and not
   community_rows:`. `test_activate_cross_tenant_community_is_403` seeds
   user `"1"` as a GENUINE active admin member of the OTHER tenant's
   community (not just an absent membership row, which would pass for the
   wrong reason) and asserts 403 when acting under a JWT whose tenant claim
   doesn't own that `community_id` -- went red (201 instead of 403, real
   membership in a tenant-B community authorized an action while the
   caller's JWT/tenant context was tenant A); reverted, green again.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.app_manifest import parse_manifest
from flask_core.app_registry import get_registry
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_lifecycle import marketplace_lifecycle_bp
from services.errors import ApiError
from services.marketplace_lifecycle_service import ensure_registered
from tests.conftest import (
    LIFECYCLE_COMMUNITY_ID,
    LIFECYCLE_OTHER_COMMUNITY_ID,
    OTHER_TENANT_SLUG,
    TENANT_SLUG,
    make_user_token,
)

APP_ID_A = "waddles.bot.shoutout.custom-a"
APP_ID_B = "waddles.bot.shoutout.custom-b"


def _manifest(
    app_id: str,
    *,
    incompatible_with: list[str] | None = None,
    provider: str = "builtin",
    **attribution: Any,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "appId": app_id,
        "name": "Custom Shoutout",
        "version": "1.0.0",
        "feature": "waddles.bot.shoutout",
        "module": "bot",
        "provider": provider,
        "executionModel": "native",
        "isDefault": False,
        "compatibleWith": [],
        "incompatibleWith": incompatible_with or [],
        "platformCompatibility": {"testedWith": "release/v3.0.X"},
    }
    manifest.update(attribution)
    return manifest


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    """`AppRegistry` is a process-wide singleton -- clear it before/after every test.

    Without this, `install_bundle()`'s `reg.register(manifest)` raises
    `RegistryError` (`duplicate_app_id`) the second time any test in this
    module installs `APP_ID_A`/`APP_ID_B`, since the registry survives
    across tests within the same pytest process (unlike the per-test
    sqlite fixture).
    """
    get_registry().clear()
    yield
    get_registry().clear()


@pytest.fixture
def app(lifecycle_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(marketplace_lifecycle_bp)
    quart_app.config["dal"] = lifecycle_db.dal
    quart_app.config["async_dal"] = lifecycle_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _platform_admin_headers() -> dict[str, str]:
    token = make_user_token(user_id=1, scope="platform:admin", tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


def _tenant_admin_headers(*, tenant: str = TENANT_SLUG) -> dict[str, str]:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


def _community_admin_headers(*, tenant: str = TENANT_SLUG) -> dict[str, str]:
    """User `"1"` -- `lifecycle_db` seeds this exact id as a `community_roles` admin."""
    token = make_user_token(user_id=1, scope="", tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


async def _install(client: Any, app_id: str = APP_ID_A, **kw: Any) -> Any:
    return await client.post(
        "/api/v1/marketplace/bundles",
        headers=_platform_admin_headers(),
        json=_manifest(app_id, **kw),
    )


class TestAttributionMetadata:
    """`author`/`license`/`source_url`/`alternative_to`/`homepage_url`/`notice`/`category`.

    Round-trips the migration-0026 attribution block through
    `POST /bundles` -> `app_catalog` -> `GET /bundles`'s `BundleDTO`, and
    exercises the SPDX-allowlist + https-only-URL shape rules
    `flask_core.app_manifest.parse_manifest` enforces on whichever fields
    ARE present. `author`/`license` are never REQUIRED at this endpoint --
    it is `platform:admin`-gated (not `vendor:onboard`), so it is not the
    vendor-onboarding path; that mandatory-for-vendors gate is exercised in
    `hub_api/tests/test_bundle_manifest_v2.py` instead, against the schema
    the real vendor-upload endpoint (`bundle_version_service.create_version`)
    actually validates against.
    """

    async def test_builtin_without_attribution_still_installs(self, client: Any) -> None:
        """A first-party `builtin` bundle never has to declare the block at all."""
        response = await _install(client)
        assert response.status_code == 201

    async def test_thirdparty_provider_without_attribution_still_installs(
        self, client: Any
    ) -> None:
        """`provider: thirdparty` alone (admin-installed) needs no attribution here."""
        response = await _install(client, provider="thirdparty")
        assert response.status_code == 201

    async def test_unknown_spdx_license_is_400(self, client: Any) -> None:
        response = await _install(
            client, provider="thirdparty", author="Acme Corp", license="Not-A-Real-License"
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert "unknown_spdx_license" in body["error"]["message"]

    async def test_http_source_url_is_400(self, client: Any) -> None:
        response = await _install(
            client,
            provider="thirdparty",
            author="Acme Corp",
            license="MIT",
            sourceUrl="http://example.com/repo",
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert "invalid_source_url" in body["error"]["message"]

    async def test_invalid_category_is_400(self, client: Any) -> None:
        response = await _install(client, category="not-a-real-category")
        assert response.status_code == 400
        body = await response.get_json()
        assert "invalid_category" in body["error"]["message"]

    async def test_vendor_attribution_round_trips_to_catalog_listing(self, client: Any) -> None:
        response = await _install(
            client,
            provider="thirdparty",
            author="Acme Corp",
            license="MIT",
            sourceUrl="https://github.com/acme/waddles-bundle",
            homepageUrl="https://acme.example.com",
            alternativeTo=["waddles.bot.shoutout.builtin"],
            notice="Portions (c) Acme Corp, under MIT.",
            category="alternatives",
        )
        assert response.status_code == 201

        listing = await client.get(
            "/api/v1/marketplace/bundles?category=alternatives", headers=_tenant_admin_headers()
        )
        assert listing.status_code == 200
        body = await listing.get_json()
        bundle = body["bundles"][0]
        assert bundle["appId"] == APP_ID_A
        assert bundle["author"] == "Acme Corp"
        assert bundle["license"] == "MIT"
        assert bundle["licenseReviewRequired"] is False
        assert bundle["sourceUrl"] == "https://github.com/acme/waddles-bundle"
        assert bundle["homepageUrl"] == "https://acme.example.com"
        assert bundle["alternativeTo"] == ["waddles.bot.shoutout.builtin"]
        assert bundle["notice"] == "Portions (c) Acme Corp, under MIT."
        assert bundle["category"] == "alternatives"

    async def test_copyleft_license_flags_review_required_but_installs(self, client: Any) -> None:
        response = await _install(
            client,
            provider="thirdparty",
            author="Acme Corp",
            license="GPL-3.0-only",
        )
        assert response.status_code == 201

        listing = await client.get("/api/v1/marketplace/bundles", headers=_tenant_admin_headers())
        body = await listing.get_json()
        assert body["bundles"][0]["licenseReviewRequired"] is True

    async def test_category_filter_excludes_non_matching_bundles(self, client: Any) -> None:
        await _install(client, app_id=APP_ID_A, category="alternatives")
        await _install(client, app_id=APP_ID_B)

        listing = await client.get(
            "/api/v1/marketplace/bundles?category=alternatives", headers=_tenant_admin_headers()
        )
        body = await listing.get_json()
        assert [b["appId"] for b in body["bundles"]] == [APP_ID_A]


async def _make_available(client: Any, app_id: str = APP_ID_A) -> Any:
    return await client.post(
        f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles",
        headers=_tenant_admin_headers(),
        json={"appId": app_id},
    )


async def _activate(
    client: Any, app_id: str = APP_ID_A, community_id: int = LIFECYCLE_COMMUNITY_ID
) -> Any:
    return await client.post(
        f"/api/v1/marketplace/community/{community_id}/bundles",
        headers=_community_admin_headers(),
        json={"appId": app_id},
    )


class TestInstallUninstall:
    async def test_install_no_token_is_401(self, client: Any) -> None:
        response = await client.post("/api/v1/marketplace/bundles", json=_manifest(APP_ID_A))
        assert response.status_code == 401

    async def test_install_wrong_scope_is_403(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/marketplace/bundles",
            headers=_tenant_admin_headers(),
            json=_manifest(APP_ID_A),
        )
        assert response.status_code == 403

    async def test_install_success_is_201_and_listed(self, client: Any) -> None:
        response = await _install(client)
        assert response.status_code == 201

        listing = await client.get("/api/v1/marketplace/bundles", headers=_tenant_admin_headers())
        assert listing.status_code == 200
        body = await listing.get_json()
        assert body["bundles"][0]["appId"] == APP_ID_A
        assert body["bundles"][0]["status"] == "active"

    async def test_install_duplicate_is_409(self, client: Any) -> None:
        first = await _install(client)
        assert first.status_code == 201
        second = await _install(client)
        assert second.status_code == 409

    async def test_install_bad_manifest_is_400(self, client: Any) -> None:
        bad = _manifest(APP_ID_A)
        bad["appId"] = "not-namespaced"
        response = await client.post(
            "/api/v1/marketplace/bundles", headers=_platform_admin_headers(), json=bad
        )
        assert response.status_code == 400

    async def test_uninstall_sets_status_yanked(self, client: Any) -> None:
        await _install(client)
        response = await client.delete(
            f"/api/v1/marketplace/bundles/{APP_ID_A}", headers=_platform_admin_headers()
        )
        assert response.status_code == 200

        listing = await client.get(
            "/api/v1/marketplace/bundles?status=yanked", headers=_tenant_admin_headers()
        )
        body = await listing.get_json()
        assert body["bundles"][0]["appId"] == APP_ID_A

    async def test_uninstall_unknown_is_404(self, client: Any) -> None:
        response = await client.delete(
            "/api/v1/marketplace/bundles/waddles.bot.shoutout.nope",
            headers=_platform_admin_headers(),
        )
        assert response.status_code == 404


class TestMakeAvailable:
    async def test_make_available_without_install_is_409(self, client: Any) -> None:
        """Superset invariant #1 (`available <= installed`) -- see module fail-first note."""
        response = await _make_available(client)
        assert response.status_code == 409

    async def test_make_available_no_token_is_401(self, client: Any) -> None:
        await _install(client)
        response = await client.post(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles", json={"appId": APP_ID_A}
        )
        assert response.status_code == 401

    async def test_make_available_wrong_tenant_slug_is_403(self, client: Any) -> None:
        await _install(client)
        response = await client.post(
            f"/api/v1/marketplace/tenant/{OTHER_TENANT_SLUG}/bundles",
            headers=_tenant_admin_headers(tenant=TENANT_SLUG),
            json={"appId": APP_ID_A},
        )
        assert response.status_code == 403

    async def test_make_available_success_is_201_and_listed(self, client: Any) -> None:
        await _install(client)
        response = await _make_available(client)
        assert response.status_code == 201

        listing = await client.get(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles", headers=_tenant_admin_headers()
        )
        body = await listing.get_json()
        assert body["bundles"][0]["appId"] == APP_ID_A
        assert body["bundles"][0]["available"] is True

    async def test_make_unavailable_then_list_filters_it_out(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        response = await client.delete(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles/{APP_ID_A}",
            headers=_tenant_admin_headers(),
        )
        assert response.status_code == 200

        listing = await client.get(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles?available=true",
            headers=_tenant_admin_headers(),
        )
        body = await listing.get_json()
        assert body["bundles"] == []

    async def test_make_unavailable_unknown_is_404(self, client: Any) -> None:
        response = await client.delete(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles/{APP_ID_A}",
            headers=_tenant_admin_headers(),
        )
        assert response.status_code == 404


class TestActivateDeactivate:
    async def test_activate_without_availability_is_409(self, client: Any) -> None:
        """Superset invariant #2 (`activated <= available`) -- see module fail-first note."""
        await _install(client)
        response = await _activate(client)
        assert response.status_code == 409

    async def test_activate_no_membership_is_403(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        token = make_user_token(user_id=999, scope="", tenant=TENANT_SLUG)
        response = await client.post(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles",
            headers={"Authorization": f"Bearer {token}"},
            json={"appId": APP_ID_A},
        )
        assert response.status_code == 403

    async def test_activate_cross_tenant_community_is_403(
        self, client: Any, lifecycle_db: Any
    ) -> None:
        """Per-community IDOR -- see module fail-first note #3.

        User `"1"` is ALSO seeded here as a genuine active admin member of
        `LIFECYCLE_OTHER_COMMUNITY_ID` (the OTHER tenant's community) --
        without that, this test would pass for the wrong reason (no
        membership row at all), never actually exercising
        `resolve_community_membership_scoped`'s tenant-ownership guard
        (`community.tenant_id == ctx.tenant_id`, checked BEFORE the
        `community_members` lookup). The caller's JWT still claims
        `TENANT_SLUG` (tenant A) -- real membership in a tenant-B community
        must not authorize action there while acting under tenant A's
        JWT/tenant context.
        """
        dal = lifecycle_db.dal
        role_id = dal.community_roles.insert(
            community_id=LIFECYCLE_OTHER_COMMUNITY_ID,
            name="admin",
            base_claims={"scopes": ["community:manage_members"]},
        )
        dal.community_members.insert(
            community_id=LIFECYCLE_OTHER_COMMUNITY_ID,
            user_id="1",
            role="admin",
            community_role_id=role_id,
            is_active=True,
        )
        dal.commit()

        await _install(client)
        await _make_available(client)
        response = await _activate(client, community_id=LIFECYCLE_OTHER_COMMUNITY_ID)
        assert response.status_code == 403

    async def test_activate_success_is_201_and_listed(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        response = await _activate(client)
        assert response.status_code == 201

        listing = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles",
            headers=_community_admin_headers(),
        )
        body = await listing.get_json()
        assert body["bundles"][0]["appId"] == APP_ID_A
        assert body["bundles"][0]["enabled"] is True

    async def test_activate_conflicting_bundle_is_409(self, client: Any) -> None:
        await _install(client, APP_ID_A, incompatible_with=[APP_ID_B])
        await _install(client, APP_ID_B)
        await _make_available(client, APP_ID_A)
        await _make_available(client, APP_ID_B)
        first = await _activate(client, APP_ID_A)
        assert first.status_code == 201
        second = await _activate(client, APP_ID_B)
        assert second.status_code == 409

    async def test_deactivate_success(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        await _activate(client)
        response = await client.delete(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles/{APP_ID_A}",
            headers=_community_admin_headers(),
        )
        assert response.status_code == 200

        listing = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles?enabled=true",
            headers=_community_admin_headers(),
        )
        body = await listing.get_json()
        assert body["bundles"] == []

    async def test_deactivate_unknown_is_404(self, client: Any) -> None:
        response = await client.delete(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles/{APP_ID_A}",
            headers=_community_admin_headers(),
        )
        assert response.status_code == 404


class TestListingFiltersAndPagination:
    async def test_list_installed_filters_by_module(self, client: Any) -> None:
        await _install(client, APP_ID_A)
        response = await client.get(
            "/api/v1/marketplace/bundles?module=social", headers=_tenant_admin_headers()
        )
        body = await response.get_json()
        assert body["bundles"] == []

    async def test_list_installed_pagination(self, client: Any) -> None:
        await _install(client, APP_ID_A)
        await _install(client, APP_ID_B)
        response = await client.get(
            "/api/v1/marketplace/bundles?page=1&limit=1", headers=_tenant_admin_headers()
        )
        body = await response.get_json()
        assert len(body["bundles"]) == 1
        assert body["pagination"] == {"page": 1, "limit": 1, "total": 2, "totalPages": 2}

    async def test_list_installed_filters_by_feature_and_provider(self, client: Any) -> None:
        await _install(client, APP_ID_A)
        hit = await client.get(
            "/api/v1/marketplace/bundles?feature=waddles.bot.shoutout&provider=builtin",
            headers=_tenant_admin_headers(),
        )
        assert len((await hit.get_json())["bundles"]) == 1
        miss = await client.get(
            "/api/v1/marketplace/bundles?provider=thirdparty", headers=_tenant_admin_headers()
        )
        assert (await miss.get_json())["bundles"] == []

    async def test_list_available_filters_by_module_and_feature(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        hit = await client.get(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles?module=bot&feature=waddles.bot.shoutout",
            headers=_tenant_admin_headers(),
        )
        assert len((await hit.get_json())["bundles"]) == 1
        miss = await client.get(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles?module=social",
            headers=_tenant_admin_headers(),
        )
        assert (await miss.get_json())["bundles"] == []

    async def test_list_available_wrong_tenant_slug_is_403(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/marketplace/tenant/{OTHER_TENANT_SLUG}/bundles",
            headers=_tenant_admin_headers(tenant=TENANT_SLUG),
        )
        assert response.status_code == 403

    async def test_list_activated_no_membership_is_403(self, client: Any) -> None:
        token = make_user_token(user_id=999, scope="", tenant=TENANT_SLUG)
        response = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_list_activated_filters_by_module_and_feature(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        await _activate(client)
        hit = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles"
            "?module=bot&feature=waddles.bot.shoutout",
            headers=_community_admin_headers(),
        )
        assert len((await hit.get_json())["bundles"]) == 1
        miss = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles?module=social",
            headers=_community_admin_headers(),
        )
        assert (await miss.get_json())["bundles"] == []


class TestUpsertAndRegistryEdgeCases:
    async def test_make_available_re_enable_after_disable_is_upsert(self, client: Any) -> None:
        """Exercises `make_available()`'s UPDATE branch (row already exists, disabled)."""
        await _install(client)
        await _make_available(client)
        await client.delete(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles/{APP_ID_A}",
            headers=_tenant_admin_headers(),
        )
        response = await _make_available(client)
        assert response.status_code == 201

        listing = await client.get(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles", headers=_tenant_admin_headers()
        )
        body = await listing.get_json()
        assert body["bundles"][0]["available"] is True

    async def test_make_available_on_yanked_bundle_is_409(self, client: Any) -> None:
        """`make_available()`'s own `status == 'active'` check (beyond the shared invariant)."""
        await _install(client)
        await client.delete(
            f"/api/v1/marketplace/bundles/{APP_ID_A}", headers=_platform_admin_headers()
        )
        response = await _make_available(client)
        assert response.status_code == 409

    async def test_activate_re_enable_after_deactivate_is_upsert(self, client: Any) -> None:
        """Exercises `activate_bundle()`'s UPDATE branch (row already exists, disabled)."""
        await _install(client)
        await _make_available(client)
        await _activate(client)
        await client.delete(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles/{APP_ID_A}",
            headers=_community_admin_headers(),
        )
        response = await _activate(client)
        assert response.status_code == 201

        listing = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/bundles",
            headers=_community_admin_headers(),
        )
        body = await listing.get_json()
        assert body["bundles"][0]["enabled"] is True

    async def test_install_conflicting_with_stale_registry_entry_is_409(self, client: Any) -> None:
        """`install_bundle()`'s `RegistryError` branch: registered in-memory but no DB row yet.

        Simulates two hub-api replicas racing to install the same bundle --
        this process's own `AppRegistry` singleton already has `app_id`
        (e.g. from a concurrent request that beat this one to
        `reg.register()` but hasn't committed its DB row yet), so the DB-level
        duplicate check (line ~137) passes, but the registry's own
        duplicate-id guard still correctly rejects it.
        """
        get_registry().register(
            parse_manifest(
                {
                    "app_id": APP_ID_A,
                    "name": "Custom Shoutout",
                    "version": "1.0.0",
                    "feature": "waddles.bot.shoutout",
                    "module": "bot",
                    "provider": "builtin",
                }
            )
        )
        response = await _install(client, APP_ID_A)
        assert response.status_code == 409

    async def test_ensure_registered_self_heals_after_registry_clear(self, client: Any) -> None:
        """`ensure_registered()`'s lazy DB-reconstruction path (simulates a hub-api restart).

        `_clear_registry` clears the singleton between tests, but NOT
        mid-test -- clearing it here, after install+make_available (both of
        which already wrote real `app_catalog`/`app_tenant_availability`
        rows), simulates the in-memory `AppRegistry` resetting on a real
        restart while the DB rows persist. `activate_bundle()`'s
        `ensure_registered()` call must still succeed by re-`parse_manifest`-ing
        straight from the `app_catalog` row.
        """
        await _install(client)
        await _make_available(client)
        get_registry().clear()

        response = await _activate(client)
        assert response.status_code == 201


class TestEnsureRegisteredDirect:
    """Direct unit coverage of `ensure_registered()`'s 404 branch (unknown to registry AND DB)."""

    async def test_unknown_app_id_raises_not_found(self, lifecycle_db: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            await ensure_registered(lifecycle_db.dal, "waddles.bot.shoutout.nope")
        assert exc_info.value.status_code == 404


class TestResolvedBundlesNoBinding:
    async def test_resolved_skips_feature_with_no_enabled_binding(self, client: Any) -> None:
        """`resolve_community_bundles()`'s `except Exception: continue` branch.

        A0 is installed + made available then immediately made UNAVAILABLE
        again (never activated) -- the only `app_tenant_availability` row
        for its feature is `available=False`, so `resolve_apps` finds
        nothing enabled and no default is registered for that feature,
        raising `BindingError`; the resolved endpoint must skip it rather
        than 500.
        """
        await _install(client)
        await _make_available(client)
        await client.delete(
            f"/api/v1/marketplace/tenant/{TENANT_SLUG}/bundles/{APP_ID_A}",
            headers=_tenant_admin_headers(),
        )
        response = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/resolved",
            headers=_community_admin_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["apps"] == []
        assert body["conflicts"] == []


class TestResolvedBundles:
    async def test_resolved_no_membership_is_403(self, client: Any) -> None:
        token = make_user_token(user_id=999, scope="", tenant=TENANT_SLUG)
        response = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/resolved",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_resolved_returns_activated_app_no_conflicts(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        await _activate(client)
        response = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/resolved",
            headers=_community_admin_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert [a["appId"] for a in body["apps"]] == [APP_ID_A]
        assert body["conflicts"] == []

    async def test_resolved_surfaces_conflict_between_activated_and_available(
        self, client: Any
    ) -> None:
        """B is only tenant-available (never activated) but still resolves tenant-wide.

        Activating A and B together is blocked at write time (409, see
        `test_activate_conflicting_bundle_is_409`) -- this proves the
        RESOLVED view still surfaces an emergent conflict when A is
        community-activated and B reaches the same community only via the
        tenant-wide "available" fallback (`DBInstallationLookup`'s own
        documented interpretation, see its module docstring).
        """
        await _install(client, APP_ID_A, incompatible_with=[APP_ID_B])
        await _install(client, APP_ID_B)
        await _make_available(client, APP_ID_A)
        await _make_available(client, APP_ID_B)
        await _activate(client, APP_ID_A)

        response = await client.get(
            f"/api/v1/marketplace/community/{LIFECYCLE_COMMUNITY_ID}/resolved",
            headers=_community_admin_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        app_ids = {a["appId"] for a in body["apps"]}
        assert app_ids == {APP_ID_A, APP_ID_B}
        assert {"appId": APP_ID_A, "conflictsWithAppId": APP_ID_B} in body["conflicts"]


class TestCrossTenantIsolation:
    """Confirms the `lifecycle_db` fixture's second tenant/community are genuinely isolated."""

    async def test_other_tenant_cannot_see_first_tenants_availability(self, client: Any) -> None:
        await _install(client)
        await _make_available(client)
        response = await client.get(
            f"/api/v1/marketplace/tenant/{OTHER_TENANT_SLUG}/bundles",
            headers=_tenant_admin_headers(tenant=OTHER_TENANT_SLUG),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["bundles"] == []
