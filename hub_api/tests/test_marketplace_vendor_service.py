"""`services/marketplace_vendor_service.py` -- unit tests for uncovered branches.

Covers what the blueprint tests' happy/security paths don't reach:
multi-field profile updates, slug-collision retry, missing-field
validation, and revenue aggregation.
"""

from __future__ import annotations

from typing import Any

import pytest

from services import marketplace_vendor_service as vendor
from services.errors import ApiError
from services.schema import bind_marketplace_module_tables, bind_marketplace_vendor_tables


@pytest.fixture
def dal() -> Any:
    from pydal import DAL

    db = DAL("sqlite:memory")
    bind_marketplace_module_tables(db, migrate=True)
    bind_marketplace_vendor_tables(db, migrate=True)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: Any) -> None:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
    )


class TestUpdateVendorProfileAllFields:
    def test_updates_description_website_and_payout_method(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        result = vendor.update_vendor_profile(
            dal,
            1,
            {
                "description": "new desc",
                "websiteUrl": "https://new.example.com",
                "payoutMethod": "paypal",
            },
        )
        assert result["description"] == "new desc"
        assert result["websiteUrl"] == "https://new.example.com"
        assert result["payoutMethod"] == "paypal"


class TestGetVendorModulesNoProfile:
    def test_no_profile_returns_empty(self, dal: Any) -> None:
        result = vendor.get_vendor_modules(dal, 999)
        assert result == {"modules": [], "pagination": {"page": 1, "limit": 25, "total": 0}}


class TestDashboardRevenueBreakdown:
    def test_revenue_grouped_by_module(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        module = vendor.create_vendor_module(
            dal,
            1,
            {
                "name": "Mod",
                "webhookUrl": "https://vendor.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        seller = vendor.get_vendor_profile(dal, 1)
        dal.vendor_payments.insert(
            seller_id=seller["id"], module_id=module["id"], amount_cents=300, status="completed"
        )
        dal.vendor_payments.insert(
            seller_id=seller["id"], module_id=module["id"], amount_cents=200, status="pending"
        )
        dal.commit()

        dashboard = vendor.get_vendor_dashboard(dal, 1)
        assert dashboard["revenueBreakdown"][0]["moduleId"] == module["id"]
        assert dashboard["revenueBreakdown"][0]["revenue"] == 500
        assert dashboard["stats"]["totalRevenue"] == 300
        assert dashboard["stats"]["expectedRevenue"] == 200


class TestCreateVendorModuleValidation:
    def test_missing_webhook_url_raises(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        with pytest.raises(ApiError) as exc_info:
            vendor.create_vendor_module(dal, 1, {"name": "Mod"})
        assert exc_info.value.status_code == 400

    def test_missing_name_raises(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        with pytest.raises(ApiError) as exc_info:
            vendor.create_vendor_module(dal, 1, {"webhookUrl": "https://vendor.example.com/hook"})
        assert exc_info.value.status_code == 400

    def test_slug_collision_retries_with_suffix(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        vendor.create_vendor_profile(dal, 2, {"displayName": "B"})
        first = vendor.create_vendor_module(
            dal,
            1,
            {
                "name": "Same Name",
                "webhookUrl": "https://vendor.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        second = vendor.create_vendor_module(
            dal,
            2,
            {
                "name": "Same Name",
                "webhookUrl": "https://vendor2.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        assert first["slug"] != second["slug"]
        assert second["slug"] == "same-name-2"

    def test_ssrf_guard_applies_to_api_base_url_too(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        with pytest.raises(ApiError):
            vendor.create_vendor_module(
                dal,
                1,
                {
                    "name": "Mod",
                    "webhookUrl": "https://vendor.example.com/hook",
                    "apiBaseUrl": "http://127.0.0.1/internal",
                },
            )


class TestUpdateVendorModuleEdgeCases:
    def test_no_fields_to_update_returns_success_only(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        module = vendor.create_vendor_module(
            dal,
            1,
            {
                "name": "Mod",
                "webhookUrl": "https://vendor.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        result = vendor.update_vendor_module(dal, 1, module["id"], {})
        assert result == {"success": True}

    def test_ssrf_guard_on_webhook_url_update(self, dal: Any) -> None:
        vendor.create_vendor_profile(dal, 1, {"displayName": "A"})
        module = vendor.create_vendor_module(
            dal,
            1,
            {
                "name": "Mod",
                "webhookUrl": "https://vendor.example.com/hook",
                "webhookSecret": "s3cret",
            },
        )
        with pytest.raises(ApiError):
            vendor.update_vendor_module(
                dal, 1, module["id"], {"webhookUrl": "http://169.254.169.254/latest"}
            )


class TestCreateVendorRequestValidation:
    def test_missing_required_fields_raises(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            vendor.create_vendor_request(dal, 1, "a@example.com", "Alice", {})
        assert exc_info.value.status_code == 400

    def test_existing_approved_request_blocks_new_one(self, dal: Any) -> None:
        payload = {
            "companyName": "Co",
            "businessDescription": "desc",
            "contactEmail": "a@example.com",
        }
        vendor.create_vendor_request(dal, 1, "a@example.com", "Alice", payload)
        row = dal(dal.vendor_role_requests.user_id == 1).select().first()
        dal(dal.vendor_role_requests.id == row.id).update(status="approved")
        dal.commit()

        with pytest.raises(ApiError) as exc_info:
            vendor.create_vendor_request(dal, 1, "a@example.com", "Alice", payload)
        assert exc_info.value.status_code == 400
        assert "already has an approved" in exc_info.value.message
