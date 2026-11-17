"""`blueprints/v1/marketplace_admin_review.py` -- admin review + internal integration.

Standalone Quart app registering only `admin_review_bp`/`internal_bp`
against the `marketplace_db` fixture.

Fail-first proof (executed, not narrated) for self-approval: temporarily
removed the `_reject_self_approval(...)` call from both
`approve_vendor_role_request` and `approve_submission`
(`services/marketplace_review_service.py`) -- `test_admin_cannot_approve_
own_vendor_role_request` and `test_admin_cannot_approve_own_submission`
both went red (200 instead of 403, an admin approved their own prior
request/submission); reverted, green again.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_admin_review import admin_review_bp, internal_bp
from tests.conftest import make_user_token


@pytest.fixture
def app(marketplace_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(admin_review_bp)
    quart_app.register_blueprint(internal_bp)
    quart_app.config["dal"] = marketplace_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(dal: Any, *, username: str = "admin", email: str = "admin@example.com") -> int:
    user_id: int = dal.hub_users.insert(username=username, email=email, display_name=username)
    dal.commit()
    return user_id


def _admin_headers(user_id: int) -> dict[str, str]:
    token = make_user_token(user_id=user_id, scope="marketplace:admin")
    return {"Authorization": f"Bearer {token}"}


class TestAuthBypass:
    async def test_vendor_requests_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/admin/marketplace/vendor-requests")
        assert response.status_code == 401


class TestScopeCheck:
    async def test_vendor_requests_wrong_scope_is_403(
        self, client: Any, marketplace_db: Any
    ) -> None:
        user_id = _seed_user(marketplace_db)
        token = make_user_token(user_id=user_id, scope="marketplace:read")
        response = await client.get(
            "/api/v1/marketplace/admin/marketplace/vendor-requests",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403


class TestSelfApprovalDenied:
    async def test_admin_cannot_approve_own_vendor_role_request(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        marketplace_db.vendor_role_requests.insert(
            request_id="req-self-1",
            user_id=admin_id,
            company_name="Self Co",
            business_description="desc",
            contact_email="admin@example.com",
            status="pending",
        )
        marketplace_db.commit()

        response = await client.post(
            "/api/v1/marketplace/admin/marketplace/vendor-requests/req-self-1/approve",
            headers=_admin_headers(admin_id),
            json={},
        )
        assert response.status_code == 403
        row = (
            marketplace_db(marketplace_db.vendor_role_requests.request_id == "req-self-1")
            .select()
            .first()
        )
        assert row.status == "pending"  # not silently approved

    async def test_admin_can_approve_a_different_vendors_request(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db, username="admin", email="admin@example.com")
        vendor_id = _seed_user(marketplace_db, username="vendor", email="vendor@example.com")
        marketplace_db.vendor_role_requests.insert(
            request_id="req-other-1",
            user_id=vendor_id,
            company_name="Other Co",
            business_description="desc",
            contact_email="vendor@example.com",
            status="pending",
        )
        marketplace_db.commit()

        response = await client.post(
            "/api/v1/marketplace/admin/marketplace/vendor-requests/req-other-1/approve",
            headers=_admin_headers(admin_id),
            json={},
        )
        assert response.status_code == 200
        vendor_row = marketplace_db.hub_users[vendor_id]
        assert vendor_row.is_vendor is True

    async def test_admin_cannot_approve_own_submission(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        module_id = marketplace_db.marketplace_modules.insert(
            seller_id=1,
            name="Mod",
            slug="mod",
            webhook_url="https://vendor.example.com/hook",
            webhook_secret="s3cret",
        )
        submission_id = marketplace_db.marketplace_submissions.insert(
            module_id=module_id, submitted_by=admin_id, status="pending"
        )
        marketplace_db.commit()

        response = await client.post(
            f"/api/v1/marketplace/admin/marketplace/submissions/{submission_id}/approve",
            headers=_admin_headers(admin_id),
            json={},
        )
        assert response.status_code == 403
        row = marketplace_db.marketplace_submissions[submission_id]
        assert row.status == "pending"


class TestInternalServiceAuth:
    async def test_get_commands_without_service_key_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/internal/marketplace/commands/1")
        assert response.status_code == 401

    async def test_get_commands_with_valid_service_key(
        self, client: Any, service_key_headers: dict[str, str]
    ) -> None:
        response = await client.get(
            "/api/v1/internal/marketplace/commands/1", headers=service_key_headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "commands": []}

    async def test_execute_without_service_key_is_401(self, client: Any) -> None:
        response = await client.post("/api/v1/internal/marketplace/execute/1", json={})
        assert response.status_code == 401

    async def test_execute_unknown_module_is_404(
        self, client: Any, service_key_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/internal/marketplace/execute/999", headers=service_key_headers, json={}
        )
        assert response.status_code == 404


class TestVendorRequestsListAndReject:
    async def test_list_vendor_requests(self, client: Any, marketplace_db: Any) -> None:
        admin_id = _seed_user(marketplace_db)
        marketplace_db.vendor_role_requests.insert(
            request_id="req-list-1",
            user_id=admin_id,
            company_name="Co",
            business_description="desc",
            contact_email="a@example.com",
            status="pending",
        )
        marketplace_db.commit()
        response = await client.get(
            "/api/v1/marketplace/admin/marketplace/vendor-requests",
            headers=_admin_headers(admin_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pagination"]["total"] == 1

    async def test_reject_vendor_request_requires_reason(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        vendor_id = _seed_user(marketplace_db, username="v2", email="v2@example.com")
        marketplace_db.vendor_role_requests.insert(
            request_id="req-reject-1",
            user_id=vendor_id,
            company_name="Co",
            business_description="desc",
            contact_email="v2@example.com",
            status="pending",
        )
        marketplace_db.commit()

        no_reason = await client.post(
            "/api/v1/marketplace/admin/marketplace/vendor-requests/req-reject-1/reject",
            headers=_admin_headers(admin_id),
            json={},
        )
        assert no_reason.status_code == 400

        with_reason = await client.post(
            "/api/v1/marketplace/admin/marketplace/vendor-requests/req-reject-1/reject",
            headers=_admin_headers(admin_id),
            json={"reason": "not a fit"},
        )
        assert with_reason.status_code == 200
        assert (await with_reason.get_json())["request"]["status"] == "rejected"

    async def test_approve_unknown_request_is_404(self, client: Any, marketplace_db: Any) -> None:
        admin_id = _seed_user(marketplace_db)
        response = await client.post(
            "/api/v1/marketplace/admin/marketplace/vendor-requests/does-not-exist/approve",
            headers=_admin_headers(admin_id),
            json={},
        )
        assert response.status_code == 404


class TestSubmissionsListAndReject:
    async def _seed_submission(self, dal: Any, *, submitter_id: int) -> int:
        module_id = dal.marketplace_modules.insert(
            seller_id=1,
            name="Mod",
            slug="mod",
            webhook_url="https://vendor.example.com/hook",
            webhook_secret="s3cret",
        )
        submission_id: int = dal.marketplace_submissions.insert(
            module_id=module_id, submitted_by=submitter_id, status="pending"
        )
        dal.commit()
        return submission_id

    async def test_list_submissions(self, client: Any, marketplace_db: Any) -> None:
        admin_id = _seed_user(marketplace_db)
        vendor_id = _seed_user(marketplace_db, username="v3", email="v3@example.com")
        await self._seed_submission(marketplace_db, submitter_id=vendor_id)

        response = await client.get(
            "/api/v1/marketplace/admin/marketplace/submissions",
            headers=_admin_headers(admin_id),
        )
        assert response.status_code == 200
        assert (await response.get_json())["pagination"]["total"] == 1

    async def test_reject_submission_requires_admin_scope(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        vendor_id = _seed_user(marketplace_db, username="v4", email="v4@example.com")
        submission_id = await self._seed_submission(marketplace_db, submitter_id=vendor_id)

        response = await client.post(
            f"/api/v1/marketplace/admin/marketplace/submissions/{submission_id}/reject",
            headers=_admin_headers(admin_id),
            json={"reason": "no thanks"},
        )
        assert response.status_code == 200
        row = marketplace_db.marketplace_submissions[submission_id]
        assert row.status == "rejected"

    async def test_approve_submission_by_different_admin_succeeds(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        vendor_id = _seed_user(marketplace_db, username="v5", email="v5@example.com")
        submission_id = await self._seed_submission(marketplace_db, submitter_id=vendor_id)

        response = await client.post(
            f"/api/v1/marketplace/admin/marketplace/submissions/{submission_id}/approve",
            headers=_admin_headers(admin_id),
            json={"notes": "looks good"},
        )
        assert response.status_code == 200


class TestMarketplaceSettings:
    async def test_get_and_update_settings_round_trip(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        empty = await client.get(
            "/api/v1/marketplace/admin/marketplace/settings", headers=_admin_headers(admin_id)
        )
        assert empty.status_code == 200
        assert (await empty.get_json())["settings"] == {}

        update = await client.put(
            "/api/v1/marketplace/admin/marketplace/settings",
            headers=_admin_headers(admin_id),
            json={"settings": {"platform_fee_percent": "25"}},
        )
        assert update.status_code == 200

        after = await client.get(
            "/api/v1/marketplace/admin/marketplace/settings", headers=_admin_headers(admin_id)
        )
        assert (await after.get_json())["settings"]["platform_fee_percent"] == "25"


class TestVendorSubmissionsAdminPipeline:
    async def _seed_vendor_submission(self, dal: Any) -> int:
        submission_id: int = dal.vendor_submissions.insert(
            submission_id="vs-1",
            vendor_name="Vendor Co",
            vendor_email="vendor@example.com",
            module_name="Cool Module",
            webhook_url="https://vendor.example.com/hook",
            pricing_model="flat-rate",
            payment_method="paypal",
            status="pending",
        )
        dal.commit()
        return submission_id

    async def test_list_and_detail_and_approve_and_publish(
        self, client: Any, marketplace_db: Any
    ) -> None:
        admin_id = _seed_user(marketplace_db)
        submission_id = await self._seed_vendor_submission(marketplace_db)

        listing = await client.get(
            "/api/v1/marketplace/admin/marketplace/vendor-submissions",
            headers=_admin_headers(admin_id),
        )
        assert listing.status_code == 200
        assert (await listing.get_json())["data"]["pagination"]["total"] == 1

        detail = await client.get(
            f"/api/v1/marketplace/admin/marketplace/vendor-submissions/{submission_id}",
            headers=_admin_headers(admin_id),
        )
        assert detail.status_code == 200

        approve = await client.post(
            f"/api/v1/marketplace/admin/marketplace/vendor-submissions/{submission_id}/approve",
            headers=_admin_headers(admin_id),
            json={"adminNotes": "great"},
        )
        assert approve.status_code == 200

        publish = await client.post(
            f"/api/v1/marketplace/admin/marketplace/vendor-submissions/{submission_id}/publish",
            headers=_admin_headers(admin_id),
        )
        assert publish.status_code == 200
        assert (await publish.get_json())["module"]["moduleSlug"]

    async def test_reject_requires_reason(self, client: Any, marketplace_db: Any) -> None:
        admin_id = _seed_user(marketplace_db)
        submission_id = await self._seed_vendor_submission(marketplace_db)
        response = await client.post(
            f"/api/v1/marketplace/admin/marketplace/vendor-submissions/{submission_id}/reject",
            headers=_admin_headers(admin_id),
            json={},
        )
        assert response.status_code == 400

    async def test_request_more_info(self, client: Any, marketplace_db: Any) -> None:
        admin_id = _seed_user(marketplace_db)
        submission_id = await self._seed_vendor_submission(marketplace_db)
        response = await client.post(
            f"/api/v1/marketplace/admin/marketplace/vendor-submissions/{submission_id}/request-info",
            headers=_admin_headers(admin_id),
            json={"message": "need more docs"},
        )
        assert response.status_code == 200
        row = marketplace_db.vendor_submissions[submission_id]
        assert row.status == "under-review"

    async def test_publish_requires_approved_status(self, client: Any, marketplace_db: Any) -> None:
        admin_id = _seed_user(marketplace_db)
        submission_id = await self._seed_vendor_submission(marketplace_db)  # status=pending
        response = await client.post(
            f"/api/v1/marketplace/admin/marketplace/vendor-submissions/{submission_id}/publish",
            headers=_admin_headers(admin_id),
        )
        assert response.status_code == 400


def test_bind_marketplace_vendor_tables_when_commands_already_defined(tmp_path: Any) -> None:
    """`commands` already bound by another group must not 500 `bind_marketplace_vendor_tables`."""
    from pydal import DAL, Field

    from services.schema import bind_marketplace_vendor_tables

    dal = DAL(f"sqlite://{tmp_path / 'shared.sqlite'}", auto_import=False)
    try:
        dal.define_table("commands", Field("command", "string"))
        bind_marketplace_vendor_tables(dal, migrate=True)
        assert "vendor_role_requests" in dal.tables
    finally:
        dal.close()
