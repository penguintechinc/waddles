"""Regression: output-DTO audit (`@validate_response` coverage) -- exact field sets.

Each test makes the service layer return a body carrying an EXTRA sensitive
key (what a future careless `row`-to-dict change would leak) and asserts the
route's explicit response DTO drops it, so only the intended field set is on
the wire. Fail-first: removing the `@validate_response` decorator from the
route makes the matching test red (the extra key reaches the client).
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_vendor import vendor_bp
from services import marketplace_vendor_service as vendor_svc
from tests.conftest import make_user_token

SELLER_FIELDS = {
    "id",
    "userId",
    "displayName",
    "description",
    "websiteUrl",
    "payoutMethod",
    "totalRevenueCents",
    "totalSubscribers",
    "isVerified",
    "createdAt",
    "updatedAt",
}
REQUEST_FIELDS = {
    "id",
    "requestId",
    "status",
    "companyName",
    "rejectionReason",
    "requestedAt",
    "reviewedAt",
}


@pytest.fixture
def client(marketplace_db: Any) -> Any:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(vendor_bp)
    app.config["dal"] = marketplace_db
    return app.test_client()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=1)}"}


def _seller(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "userId": 1,
        "displayName": "Acme",
        "description": None,
        "websiteUrl": None,
        "payoutMethod": "paypal",
        "totalRevenueCents": 0,
        "totalSubscribers": 0,
        "isVerified": False,
        "createdAt": None,
        "updatedAt": None,
    }
    base.update(extra)
    return base


async def test_get_vendor_profile_exact_field_set(client: Any, monkeypatch: Any) -> None:
    leaky = _seller(payment_details={"paypal_email": "p@x.io", "bank_account": "123"})
    monkeypatch.setattr(vendor_svc, "get_vendor_profile", lambda dal, uid: leaky)
    response = await client.get("/api/v1/marketplace/vendor/profile", headers=_headers())
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body["seller"]) == SELLER_FIELDS
    assert "payment_details" not in str(body)


async def test_get_vendor_request_exact_field_set(client: Any, monkeypatch: Any) -> None:
    leaky = {
        "id": 3,
        "requestId": "r",
        "status": "pending",
        "companyName": "Acme",
        "rejectionReason": None,
        "requestedAt": None,
        "reviewedAt": None,
        "contact_email": "pii@example.com",
        "contact_phone": "555-0100",
    }
    monkeypatch.setattr(vendor_svc, "get_vendor_request", lambda dal, uid: leaky)
    response = await client.get("/api/v1/marketplace/vendor/request", headers=_headers())
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body["request"]) == REQUEST_FIELDS
    assert "pii@example.com" not in str(body)
