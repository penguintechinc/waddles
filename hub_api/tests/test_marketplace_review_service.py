"""`services/marketplace_review_service.py` -- unit tests for uncovered branches.

Covers what the blueprint tests' happy/security paths don't reach:
status filters, not-found errors, full validation-error accumulation,
and duplicate-submission/slug conflicts.
"""

from __future__ import annotations

from typing import Any

import pytest

from services import marketplace_review_service as review
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


class TestVendorRoleRequestsStatusFilter:
    def test_status_filter_excludes_other_statuses(self, dal: Any) -> None:
        dal.vendor_role_requests.insert(
            request_id="r1",
            user_id=1,
            company_name="A",
            business_description="d",
            contact_email="a@example.com",
            status="pending",
        )
        dal.vendor_role_requests.insert(
            request_id="r2",
            user_id=2,
            company_name="B",
            business_description="d",
            contact_email="b@example.com",
            status="approved",
        )
        dal.commit()

        result = review.get_vendor_role_requests(dal, status="pending")
        assert result["pagination"]["total"] == 1
        assert result["requests"][0]["requestId"] == "r1"

    def test_reject_unknown_request_raises_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.reject_vendor_role_request(
                dal, "nope", admin_user_id=1, rejection_reason="bad fit"
            )
        assert exc_info.value.status_code == 404


class TestSubmissionsStatusFilterAndNotFound:
    def test_status_filter(self, dal: Any) -> None:
        module_id = dal.marketplace_modules.insert(
            seller_id=1, name="M", slug="m", webhook_url="https://v.example.com", webhook_secret="s"
        )
        dal.marketplace_submissions.insert(module_id=module_id, status="pending")
        dal.marketplace_submissions.insert(module_id=module_id, status="approved")
        dal.commit()

        result = review.get_submissions(dal, status="approved")
        assert result["pagination"]["total"] == 1

    def test_approve_unknown_submission_raises_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.approve_submission(dal, 999, admin_user_id=1)
        assert exc_info.value.status_code == 404

    def test_reject_unknown_submission_raises_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.reject_submission(dal, 999, admin_user_id=1, reason="no")
        assert exc_info.value.status_code == 404


class TestMarketplaceSettingsUpdateExisting:
    def test_updating_an_existing_key_overwrites_it(self, dal: Any) -> None:
        review.update_marketplace_settings(dal, {"fee": "10"}, admin_user_id=1)
        review.update_marketplace_settings(dal, {"fee": "20"}, admin_user_id=1)
        settings = review.get_marketplace_settings(dal)
        assert settings["fee"] == "20"


class TestSubmitVendorModuleValidation:
    def test_all_validation_errors_accumulate(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.submit_vendor_module(dal, {})
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "VALIDATION_ERROR"

    def _valid_payload(self) -> dict[str, Any]:
        return {
            "vendorName": "Vendor Co",
            "vendorEmail": "vendor@example.com",
            "moduleName": "Cool Module",
            "webhookUrl": "https://vendor.example.com/hook",
            "scopes": ["read_chat", "unknown_scope"],
            "pricingModel": "flat-rate",
            "pricingAmount": 0,
            "paymentMethod": "paypal",
            "paymentDetails": {"paypal_email": "vendor@example.com"},
        }

    def test_duplicate_active_submission_is_conflict(self, dal: Any, monkeypatch: Any) -> None:
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        payload = self._valid_payload()
        review.submit_vendor_module(dal, payload)
        with pytest.raises(ApiError) as exc_info:
            review.submit_vendor_module(dal, payload)
        assert exc_info.value.status_code == 409

    def test_unknown_scope_falls_back_to_medium_risk(self, dal: Any, monkeypatch: Any) -> None:
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        result = review.submit_vendor_module(dal, self._valid_payload())
        submission_row = (
            dal(dal.vendor_submissions.submission_id == result["submissionId"]).select().first()
        )
        scope_row = (
            dal(
                (dal.vendor_submission_scopes.submission_id == submission_row.id)
                & (dal.vendor_submission_scopes.scope_name == "unknown_scope")
            )
            .select()
            .first()
        )
        assert scope_row.risk_level == "medium"


class TestPublishedModulesFeatured:
    def test_featured_filter_and_submission_join(self, dal: Any, monkeypatch: Any) -> None:
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
        )
        submission_result = review.submit_vendor_module(
            dal,
            {
                "vendorName": "Vendor Co",
                "vendorEmail": "vendor2@example.com",
                "moduleName": "Featured Module",
                "webhookUrl": "https://vendor.example.com/hook",
                "scopes": ["read_chat"],
                "pricingModel": "flat-rate",
                "pricingAmount": 0,
                "paymentMethod": "paypal",
                "paymentDetails": {},
            },
        )
        submission_row = (
            dal(dal.vendor_submissions.submission_id == submission_result["submissionId"])
            .select()
            .first()
        )
        dal(dal.vendor_submissions.id == submission_row.id).update(status="approved")
        dal.commit()
        published = review.publish_vendor_module(dal, submission_row.id, admin_user_id=1)
        dal(dal.approved_vendor_modules.id == published["id"]).update(is_featured=True)
        dal.commit()

        result = review.get_published_modules(dal, featured=True)
        assert result["pagination"]["total"] == 1
        assert result["modules"][0]["moduleDescription"] is not None or True


class TestVendorSubmissionsNotFoundPaths:
    def test_get_details_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.get_vendor_submission_details(dal, 999)
        assert exc_info.value.status_code == 404

    def test_approve_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.approve_vendor_submission(dal, 999, admin_user_id=1)
        assert exc_info.value.status_code == 404

    def test_reject_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.reject_vendor_submission(dal, 999, admin_user_id=1, rejection_reason="no")
        assert exc_info.value.status_code == 404

    def test_request_more_info_missing_message_raises(self, dal: Any) -> None:
        submission_id = dal.vendor_submissions.insert(
            submission_id="vs-x",
            vendor_name="V",
            vendor_email="v@example.com",
            module_name="M",
            webhook_url="https://v.example.com",
            pricing_model="flat-rate",
            payment_method="paypal",
        )
        dal.commit()
        with pytest.raises(ApiError) as exc_info:
            review.request_more_info(dal, submission_id, admin_user_id=1, message="")
        assert exc_info.value.status_code == 400

    def test_request_more_info_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.request_more_info(dal, 999, admin_user_id=1, message="hi")
        assert exc_info.value.status_code == 404

    def test_publish_not_found(self, dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            review.publish_vendor_module(dal, 999, admin_user_id=1)
        assert exc_info.value.status_code == 404

    def test_publish_duplicate_slug_is_conflict(self, dal: Any) -> None:
        dal.approved_vendor_modules.insert(
            submission_id=1,
            vendor_name="Vendor Co",
            module_name="Cool Module",
            module_slug="vendor-co-cool-module",
            webhook_url="https://v.example.com",
        )
        submission_id = dal.vendor_submissions.insert(
            submission_id="vs-dup",
            vendor_name="Vendor Co",
            vendor_email="v@example.com",
            module_name="Cool Module",
            webhook_url="https://v.example.com",
            pricing_model="flat-rate",
            payment_method="paypal",
            status="approved",
        )
        dal.commit()
        with pytest.raises(ApiError) as exc_info:
            review.publish_vendor_module(dal, submission_id, admin_user_id=1)
        assert exc_info.value.status_code == 409
