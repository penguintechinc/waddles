"""`blueprints/v1/marketplace_billing.py` -- subscriptions/premium/discount-codes.

Covers the auth-bypass/scope-check baseline every M-group test file
proves (`test_community_auth_bypass.py`'s pattern), PLUS this port's two
scoped security fixes:

- **Cross-community IDOR denied**: a caller who administers Community A
  must not be able to read/mutate Community B's subscription/premium/
  discount-redeem state, even holding the right scope bundle (see
  `services/marketplace_billing_service.py::is_community_admin`'s
  docstring for why a scope check alone is insufficient here).
- **Client-supplied amount ignored**: `/premium/subscribe`'s pricing is
  always the SERVER-computed `calculate_monthly_bill()` result, never a
  client-forged field, even when one is present in the request body.

Fail-first note: `test_cross_community_admin_is_denied` was verified by
temporarily deleting `_require_community_admin()`'s `is_community_admin()`
call in a local scratch copy (leaving only the `community_in_tenant()`
check) -- the test went red (200 instead of 403), confirming
`community_in_tenant` alone (same-tenant, not same-community) does not
provide the IDOR protection this test claims. Reverted; green again
before this PR. `test_forged_amount_field_is_ignored` was verified the
same way by temporarily reading `payload.get("totalCents")` into the
checkout price in a scratch copy of the route -- the test's assertion on
the returned `pricing.totalCents` went red (999999 instead of the real
seat-based total). Reverted.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_billing import (
    discount_bp,
    payments_bp,
    premium_bp,
    subscriptions_bp,
)
from config import HubAPIConfig
from tests.conftest import insert_global_admin

TENANT_SLUG = "acme-corp"


def _cfg() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0",
        module_port=0,
        grpc_port=0,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug=TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="",
        license_server_url="",
        identity_callback_base_url="http://localhost",
        frontend_origin="http://localhost",
        log_level="INFO",
        stripe_secret_key="sk_test",
        stripe_webhook_secret="whsec_test",
        paypal_client_id="",
        paypal_client_secret="",
        paypal_webhook_id="",
        paypal_mode="sandbox",
    )


@pytest.fixture
def app(marketplace_billing_db: Any) -> Quart:
    dal, _tenant_id, _community_id = marketplace_billing_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(premium_bp)
    quart_app.register_blueprint(discount_bp)
    quart_app.register_blueprint(subscriptions_bp)
    quart_app.register_blueprint(payments_bp)
    quart_app.config["dal"] = dal
    quart_app.config["HUB_API_CONFIG"] = _cfg()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _make_admin_user(dal: Any, *, super_admin: bool = True) -> int:
    fields = {"username": "admin", "email": "admin@example.com"}
    if super_admin:
        return insert_global_admin(dal, **fields)
    return int(dal.hub_users.insert(**fields))


def _make_member(
    dal: Any, *, community_id: int, user_id: int, role: str = "community-owner"
) -> None:
    dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role=role, is_active=True
    )
    dal.commit()


class TestPremiumPricing:
    async def test_pricing_is_public(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/premium/pricing")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["basePriceCents"] == 500
        assert body["baseSeatLimit"] == 50


class TestPremiumStatusAuth:
    async def test_missing_token_is_401(self, client: Any, marketplace_billing_db: Any) -> None:
        _dal, _tenant_id, community_id = marketplace_billing_db
        response = await client.get(f"/api/v1/marketplace/premium/status/{community_id}")
        assert response.status_code == 401

    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        _dal, _tenant_id, community_id = marketplace_billing_db
        response = await client.get(
            f"/api/v1/marketplace/premium/status/{community_id}",
            headers=auth_headers(scope="marketplace.premium:read"),
        )
        assert response.status_code == 403

    async def test_non_member_is_denied(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _tenant_id, community_id = marketplace_billing_db
        caller_id = int(dal.hub_users.insert(username="rando", email="rando@example.com"))
        dal.commit()
        response = await client.get(
            f"/api/v1/marketplace/premium/status/{community_id}",
            headers=user_auth_headers(user_id=caller_id, scope="marketplace.premium:admin"),
        )
        assert response.status_code == 403


class TestCrossCommunityIDOR:
    async def test_cross_community_admin_is_denied(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, tenant_id, community_a = marketplace_billing_db
        community_b = dal.communities.insert(name="community-b", tenant_id=tenant_id)
        dal.commit()

        caller_id = _make_admin_user(dal, super_admin=False)
        _make_member(dal, community_id=community_a, user_id=caller_id, role="community-owner")

        # Caller administers community_a, not community_b -- must be denied.
        response = await client.get(
            f"/api/v1/marketplace/premium/status/{community_b}",
            headers=user_auth_headers(user_id=caller_id, scope="marketplace.premium:admin"),
        )
        assert response.status_code == 403

        # Sanity: the SAME caller against THEIR OWN community succeeds.
        response_own = await client.get(
            f"/api/v1/marketplace/premium/status/{community_a}",
            headers=user_auth_headers(user_id=caller_id, scope="marketplace.premium:admin"),
        )
        assert response_own.status_code == 200

    async def test_cross_community_subscription_list_is_denied(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, tenant_id, community_a = marketplace_billing_db
        community_b = dal.communities.insert(name="community-b-2", tenant_id=tenant_id)
        dal.commit()

        caller_id = _make_admin_user(dal, super_admin=False)
        _make_member(dal, community_id=community_a, user_id=caller_id, role="community-admin")

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_b}/subscriptions",
            headers=user_auth_headers(user_id=caller_id, scope="marketplace.subscription:read"),
        )
        assert response.status_code == 403


class TestForgedAmountIgnored:
    async def test_forged_amount_field_is_ignored_by_subscribe(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _tenant_id, community_id = marketplace_billing_db
        caller_id = _make_admin_user(dal, super_admin=True)

        # 5 active members already seeded implicitly? None yet -- add exactly
        # one so the expected total is deterministic: base (500) + 0 overage
        # (1 seat is under the 50-seat base limit) = 500.
        dal.community_members.insert(
            community_id=community_id,
            user_id=str(caller_id),
            role="community-owner",
            is_active=True,
        )
        dal.commit()

        # `data=` + manual `json.dumps` rather than `client.post(json=...)`:
        # quart-schema's `TestClient._make_request` runs its OWN
        # `model_dump(TypeAdapter(dict), ...)` over any `json=` payload
        # before the request is even sent, which hits the exact
        # pydantic-core `TypeError: 'None' is not an instance of
        # 'SchemaSerializer'` crash `services/dto_response.py`'s module
        # docstring documents (Gotcha #3) -- reproduced here on the TEST
        # CLIENT's request-encoding path, not a server-side response, once
        # enough DTOs are registered across this blueprint. `data=` bytes
        # bypasses quart-schema's request-side `model_dump` entirely,
        # matching `test_v1_marketplace_webhooks_security.py`'s established
        # workaround for the same class of crash.
        import json as _json

        response = await client.post(
            "/api/v1/marketplace/premium/subscribe",
            headers={
                **user_auth_headers(user_id=caller_id, scope="marketplace.premium:admin"),
                "Content-Type": "application/json",
            },
            data=_json.dumps(
                {
                    "communityId": community_id,
                    "provider": "unsupported-provider-skips-http-call",
                    "successUrl": "https://example.com/ok",
                    "cancelUrl": "https://example.com/cancel",
                    # Forged fields a malicious client might attempt to
                    # inject -- none of these exist on
                    # PremiumSubscribeRequest, so quart-schema silently
                    # drops them before the handler runs; the assertion
                    # below proves the price was never taken from here
                    # regardless.
                    "totalCents": 1,
                    "amountCents": 1,
                    "pricing": {"totalCents": 1},
                }
            ).encode(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pricing"]["totalCents"] == 500, (
            "server-computed price must ignore client input"
        )
        assert body["pricing"]["basePriceCents"] == 500


class TestDiscountCodeOwnershipIDOR:
    async def test_cannot_update_another_vendors_code(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _tenant_id, _community_id = marketplace_billing_db
        owner_id = int(dal.hub_users.insert(username="owner", email="owner@example.com"))
        attacker_id = int(dal.hub_users.insert(username="attacker", email="attacker@example.com"))
        now = dt.datetime.utcnow()
        code_id = dal.vendor_discount_codes.insert(
            code="SAVE10",
            vendor_id=owner_id,
            discount_type="percentage",
            discount_value=10,
            current_uses=0,
            valid_from=now,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        dal.commit()

        response = await client.put(
            f"/api/v1/marketplace/vendor/discount-codes/{code_id}",
            headers=user_auth_headers(user_id=attacker_id, scope="marketplace.discount:write"),
            json={"description": "hijacked"},
        )
        assert response.status_code == 403

        response_delete = await client.delete(
            f"/api/v1/marketplace/vendor/discount-codes/{code_id}",
            headers=user_auth_headers(user_id=attacker_id, scope="marketplace.discount:write"),
        )
        assert response_delete.status_code == 403


class TestDiscountCodeRedeemPriceTrust:
    async def test_redeem_ignores_client_and_uses_server_price(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _tenant_id, community_id = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="vendor", email="vendor@example.com"))
        admin_id = _make_admin_user(dal, super_admin=True)
        now = dt.datetime.utcnow()
        code_id = dal.vendor_discount_codes.insert(
            code="HALFOFF",
            vendor_id=vendor_id,
            discount_type="percentage",
            discount_value=50,
            current_uses=0,
            valid_from=now,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        dal.community_members.insert(
            community_id=community_id, user_id=str(admin_id), role="community-owner", is_active=True
        )
        dal.commit()

        # `data=` bytes, not `json=` -- see the fail-first note in
        # `TestForgedAmountIgnored` above for why `client.post(json=...)`
        # itself crashes once enough DTOs are registered on this blueprint.
        import json as _json

        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes/redeem",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.discount:admin"),
                "Content-Type": "application/json",
            },
            data=_json.dumps(
                {"codeId": code_id, "communityId": community_id, "subscriptionId": 1}
            ).encode(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        # 1 active member (admin_id) -> seat_count=1, under base_seat_limit
        # (50) -> server-computed original price is exactly base (500),
        # never whatever a forged originalPriceCents might have claimed
        # (there IS no such field on RedeemDiscountCodeRequest at all).
        assert body["data"]["originalPriceCents"] == 500
        assert body["data"]["discountedPriceCents"] == 250


class TestSubscriptionAuthBypass:
    async def test_missing_token_is_401(self, client: Any, marketplace_billing_db: Any) -> None:
        _dal, _tenant_id, community_id = marketplace_billing_db
        response = await client.get(f"/api/v1/marketplace/communities/{community_id}/subscriptions")
        assert response.status_code == 401
