"""`blueprints/v1/marketplace_vendor.py` -- vendor self-service + public submission intake.

Standalone Quart app registering only `vendor_bp`/`vendor_public_bp`
(mirrors `test_v1_auth_blueprint.py`'s own pattern) against the
`marketplace_db` fixture (`tests/conftest.py`), real JWTs via
`tests.conftest.make_user_token`, real pydal queries.

Fail-first proof (executed, not narrated) for the SSRF test: temporarily
replaced `services.url_guard.validate_url` with a no-op inside
`marketplace_vendor_service.create_vendor_module` -- `test_create_module_
rejects_ssrf_webhook_url` went red (201 instead of 400, a vendor-supplied
`webhookUrl` pointing at `127.0.0.1` was accepted); reverted, green
again. Same fail-first performed for the IDOR test: temporarily dropped
the `seller_id == owner's seller.id` clause from `_owned_module_or_404`
(kept only `id == module_id`) -- `test_update_module_cross_vendor_is_
idor_safe` went red (200 instead of 404, vendor B could edit vendor A's
module); reverted, green again.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_vendor import vendor_bp, vendor_public_bp
from tests.conftest import make_user_token


@pytest.fixture
def app(marketplace_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(vendor_bp)
    quart_app.register_blueprint(vendor_public_bp)
    quart_app.config["dal"] = marketplace_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(dal: Any, *, username: str = "alice", email: str = "alice@example.com") -> int:
    user_id: int = dal.hub_users.insert(username=username, email=email, display_name=username)
    dal.commit()
    return user_id


def _headers(user_id: int) -> dict[str, str]:
    token = make_user_token(user_id=user_id)
    return {"Authorization": f"Bearer {token}"}


class TestAuthBypass:
    async def test_get_profile_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/vendor/profile")
        assert response.status_code == 401


class TestVendorProfile:
    async def test_create_get_update_profile_round_trip(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)

        create_resp = await client.post(
            "/api/v1/marketplace/vendor/profile",
            headers=headers,
            json={"displayName": "Acme Modules", "websiteUrl": "https://acme.example.com"},
        )
        assert create_resp.status_code == 201
        body = await create_resp.get_json()
        assert body["seller"]["displayName"] == "Acme Modules"

        get_resp = await client.get("/api/v1/marketplace/vendor/profile", headers=headers)
        assert get_resp.status_code == 200
        assert (await get_resp.get_json())["seller"]["displayName"] == "Acme Modules"

        update_resp = await client.put(
            "/api/v1/marketplace/vendor/profile",
            headers=headers,
            json={"displayName": "Acme Modules Inc"},
        )
        assert update_resp.status_code == 200
        assert (await update_resp.get_json())["seller"]["displayName"] == "Acme Modules Inc"

    async def test_create_profile_conflict_on_duplicate(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )
        response = await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )
        assert response.status_code == 409


class TestModuleCreationSSRF:
    async def test_create_module_rejects_ssrf_webhook_url(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )

        response = await client.post(
            "/api/v1/marketplace/vendor/modules",
            headers=headers,
            json={
                "name": "Evil Module",
                "webhookUrl": "http://127.0.0.1:8080/internal-admin",
                "webhookSecret": "s3cret",
            },
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert "Rejected module URL" in body["error"]["message"]

    async def test_create_module_accepts_public_webhook_url(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        # DNS resolution of a real public hostname is undesirable in a unit
        # test -- stub `socket.getaddrinfo` to return a known-public IP so
        # `url_guard.is_private_host` resolves deterministically offline.
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )
        response = await client.post(
            "/api/v1/marketplace/vendor/modules",
            headers=headers,
            json={
                "name": "Good Module",
                "webhookUrl": "https://vendor.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        assert response.status_code == 201


class TestModuleOwnershipIDOR:
    async def test_update_module_cross_vendor_is_idor_safe(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        vendor_a = _seed_user(marketplace_db, username="vendor_a", email="a@example.com")
        vendor_b = _seed_user(marketplace_db, username="vendor_b", email="b@example.com")
        headers_a = _headers(vendor_a)
        headers_b = _headers(vendor_b)

        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers_a, json={"displayName": "A"}
        )
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers_b, json={"displayName": "B"}
        )
        create_resp = await client.post(
            "/api/v1/marketplace/vendor/modules",
            headers=headers_a,
            json={
                "name": "Vendor A Module",
                "webhookUrl": "https://vendor.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        module_id = (await create_resp.get_json())["module"]["id"]

        # Vendor B attempts to edit Vendor A's module -- must 404, not leak
        # existence via 403, and must not actually apply the update.
        update_resp = await client.put(
            f"/api/v1/marketplace/vendor/modules/{module_id}",
            headers=headers_b,
            json={"name": "Hijacked"},
        )
        assert update_resp.status_code == 404

        submit_resp = await client.post(
            f"/api/v1/marketplace/vendor/modules/{module_id}/submit",
            headers=headers_b,
            json={},
        )
        assert submit_resp.status_code == 404


class TestVendorRequest:
    async def test_create_request_then_duplicate_is_rejected(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        payload = {
            "companyName": "Acme Inc",
            "businessDescription": "We make modules",
            "contactEmail": "vendor@example.com",
        }
        first = await client.post(
            "/api/v1/marketplace/vendor/request", headers=headers, json=payload
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/marketplace/vendor/request", headers=headers, json=payload
        )
        assert second.status_code == 400

    async def test_get_request_no_request_yet_returns_null(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        response = await client.get("/api/v1/marketplace/vendor/request", headers=_headers(user_id))
        assert response.status_code == 200
        assert (await response.get_json())["request"] is None


class TestPublicSubmissionPipeline:
    async def test_submit_then_status_lookup_requires_matching_email(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        submit_resp = await client.post(
            "/api/v1/marketplace/public/vendor/submit",
            json={
                "vendorName": "Vendor Co",
                "vendorEmail": "vendor@example.com",
                "moduleName": "Cool Module",
                "webhookUrl": "https://vendor.example.com/hook",
                "scopes": ["read_chat"],
                "pricingModel": "flat-rate",
                "pricingAmount": 0,
                "paymentMethod": "paypal",
                "paymentDetails": {"paypal_email": "vendor@example.com"},
            },
        )
        assert submit_resp.status_code == 201
        submission_id = (await submit_resp.get_json())["submission"]["submissionId"]

        # IDOR: wrong email must 404, not leak the submission.
        wrong_email = await client.get(
            f"/api/v1/marketplace/public/vendor/submissions/{submission_id}",
            query_string={"email": "attacker@example.com"},
        )
        assert wrong_email.status_code == 404

        correct = await client.get(
            f"/api/v1/marketplace/public/vendor/submissions/{submission_id}",
            query_string={"email": "vendor@example.com"},
        )
        assert correct.status_code == 200

    async def test_submit_rejects_ssrf_webhook_url(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/marketplace/public/vendor/submit",
            json={
                "vendorName": "Vendor Co",
                "vendorEmail": "vendor@example.com",
                "moduleName": "Cool Module",
                "webhookUrl": "http://169.254.169.254/latest/meta-data/",
                "scopes": ["read_chat"],
                "pricingModel": "flat-rate",
                "pricingAmount": 0,
                "paymentMethod": "paypal",
                "paymentDetails": {"paypal_email": "vendor@example.com"},
            },
        )
        assert response.status_code == 400

    async def test_get_published_modules_empty_listing(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/public/vendor/modules")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["modules"] == []


async def _setup_vendor_with_module(
    client: Any, marketplace_db: Any, monkeypatch: Any
) -> tuple[dict[str, str], int]:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
    )
    user_id = _seed_user(marketplace_db)
    headers = _headers(user_id)
    await client.post(
        "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
    )
    create_resp = await client.post(
        "/api/v1/marketplace/vendor/modules",
        headers=headers,
        json={
            "name": "My Module",
            "webhookUrl": "https://vendor.example.com/hook",
            "webhookSecret": "s3cret",
            "triggerCommands": ["!hi"],
        },
    )
    module_id = (await create_resp.get_json())["module"]["id"]
    return headers, module_id


class TestVendorModulesListAndUpdate:
    async def test_list_modules_and_update_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, module_id = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)

        list_resp = await client.get("/api/v1/marketplace/vendor/modules", headers=headers)
        assert list_resp.status_code == 200
        body = await list_resp.get_json()
        assert len(body["modules"]) == 1

        update_resp = await client.put(
            f"/api/v1/marketplace/vendor/modules/{module_id}",
            headers=headers,
            json={"description": "Updated description"},
        )
        assert update_resp.status_code == 200

        submit_resp = await client.post(
            f"/api/v1/marketplace/vendor/modules/{module_id}/submit",
            headers=headers,
            json={"changesDescription": "initial"},
        )
        assert submit_resp.status_code == 200
        assert "submissionId" in (await submit_resp.get_json())

    async def test_update_nonexistent_module_is_404(self, client: Any, marketplace_db: Any) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        for module in ("00000000-0000-4000-8000-00000000dead", "999", "not-a-uuid"):
            response = await client.put(
                f"/api/v1/marketplace/vendor/modules/{module}",
                headers=headers,
                json={"description": "x"},
            )
            assert response.status_code == 404, module  # unknown UUID, numeric, malformed

    async def test_module_and_submission_ids_are_uuids(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, module_id = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        uuid_re = r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
        import re

        assert re.match(uuid_re, module_id)
        listed = (
            await (
                await client.get("/api/v1/marketplace/vendor/modules", headers=headers)
            ).get_json()
        )["modules"]
        assert [m["id"] for m in listed] == [module_id]  # denominator: the one module
        submit = await client.post(
            f"/api/v1/marketplace/vendor/modules/{module_id}/submit", headers=headers, json={}
        )
        assert re.match(uuid_re, (await submit.get_json())["submissionId"])

    async def test_create_module_without_profile_is_403(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        user_id = _seed_user(marketplace_db)
        response = await client.post(
            "/api/v1/marketplace/vendor/modules",
            headers=_headers(user_id),
            json={"name": "X", "webhookUrl": "https://vendor.example.com/hook"},
        )
        assert response.status_code == 403


class TestVendorDashboardAndAnalytics:
    async def test_dashboard_without_profile_is_404(self, client: Any, marketplace_db: Any) -> None:
        user_id = _seed_user(marketplace_db)
        response = await client.get(
            "/api/v1/marketplace/vendor/dashboard", headers=_headers(user_id)
        )
        assert response.status_code == 404

    async def test_dashboard_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get("/api/v1/marketplace/vendor/dashboard", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stats"]["totalModules"] == 1

    async def test_analytics_overview_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/overview", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["analytics"]["totalInstalls"] == 0

    async def test_sales_metrics_no_profile_is_404(self, client: Any, marketplace_db: Any) -> None:
        user_id = _seed_user(marketplace_db)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/sales", headers=_headers(user_id)
        )
        assert response.status_code == 404

    async def test_sales_metrics_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/sales",
            headers=headers,
            query_string={"period": "7d"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["period"] == "7d"

    async def test_install_time_series_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/installs", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["data"] == []

    async def test_api_usage_metrics_placeholder(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/api-usage", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["data"]["placeholder"] is True

    async def test_discount_codes_empty(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/discount-codes", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["data"]["codes"] == []

    async def test_community_drilldown_invalid_module_id_is_400(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/communities",
            headers=headers,
            query_string={"moduleId": "not-a-number"},
        )
        assert response.status_code == 400

    async def test_community_drilldown_empty(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/communities", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["data"]["rows"] == []

    async def test_export_csv_invalid_type_is_400(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/export",
            headers=headers,
            query_string={"type": "bogus"},
        )
        assert response.status_code == 400

    async def test_export_csv_sales_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/export",
            headers=headers,
            query_string={"type": "sales"},
        )
        assert response.status_code == 200
        assert response.content_type.startswith("text/csv")
        body_text = (await response.get_data()).decode()
        assert "metric,value" in body_text

    async def test_export_csv_installs_happy_path(
        self, client: Any, marketplace_db: Any, monkeypatch: Any
    ) -> None:
        headers, _ = await _setup_vendor_with_module(client, marketplace_db, monkeypatch)
        response = await client.get(
            "/api/v1/marketplace/vendor/analytics/export",
            headers=headers,
            query_string={"type": "installs"},
        )
        assert response.status_code == 200


class TestUpdateVendorProfileValidation:
    async def test_update_profile_blank_display_name_is_400(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )
        response = await client.put(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "   "}
        )
        assert response.status_code == 400

    async def test_update_profile_bad_website_url_is_400(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )
        response = await client.put(
            "/api/v1/marketplace/vendor/profile",
            headers=headers,
            json={"websiteUrl": "not-a-url"},
        )
        assert response.status_code == 400

    async def test_update_profile_bad_payout_method_is_400(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        headers = _headers(user_id)
        await client.post(
            "/api/v1/marketplace/vendor/profile", headers=headers, json={"displayName": "A"}
        )
        response = await client.put(
            "/api/v1/marketplace/vendor/profile",
            headers=headers,
            json={"payoutMethod": "cash-under-the-table"},
        )
        assert response.status_code == 400

    async def test_update_profile_no_existing_profile_is_404(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        response = await client.put(
            "/api/v1/marketplace/vendor/profile",
            headers=_headers(user_id),
            json={"displayName": "A"},
        )
        assert response.status_code == 404
