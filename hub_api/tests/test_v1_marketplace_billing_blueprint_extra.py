"""`blueprints/v1/marketplace_billing.py` -- remaining route coverage (success + error paths).

Complements `test_v1_marketplace_billing_blueprint.py` (auth-bypass, IDOR,
price-trust) with the success/validation-error paths for every route not
already exercised there: premium status/cancel, discount-code CRUD/
validate/redeem-error, community subscriptions CRUD, and generic payments
(checkout/cancel/refund/providers). Stripe/PayPal outbound calls are
monkeypatched at the `services.stripe_service`/`services.paypal_service`
module level (already unit-tested for real wiring in
`test_stripe_service_outbound.py`/`test_paypal_service_outbound.py`) --
this file's job is proving the BLUEPRINT wires them correctly, not
re-proving the HTTP client code.
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
        default_tenant_slug="acme-corp",
        posthog_api_key=None,
        posthog_host="",
        license_server_url="",
        identity_callback_base_url="http://localhost",
        frontend_origin="http://localhost",
        log_level="INFO",
        stripe_secret_key="sk_test",
        stripe_webhook_secret="whsec_test",
        paypal_client_id="pp_cid",
        paypal_client_secret="pp_secret",
        paypal_webhook_id="pp_wh",
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


def _admin(dal: Any) -> int:
    return insert_global_admin(dal, username="admin", email="admin@x.com")


class TestPremiumStatusAndCancel:
    async def test_status_success_no_subscription(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.get(
            f"/api/v1/marketplace/premium/status/{community_id}",
            headers=user_auth_headers(user_id=admin_id, scope="marketplace.premium:admin"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["subscription"] is None
        assert body["currentSeatCount"] == 0

    async def test_cancel_without_subscription_is_404(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/premium/cancel",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.premium:admin"),
                "Content-Type": "application/json",
            },
            data=b'{"communityId": %d, "immediately": false}' % community_id,
        )
        assert response.status_code == 404

    async def test_cancel_immediately_with_stripe_calls_provider(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, tenant_id, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        from services import marketplace_billing_service as svc

        svc.upsert_trialing_premium_subscription(dal, community_id, tenant_id, 500, 10, 50, 1)
        dal(dal.community_premium_subscriptions.community_id == community_id).update(
            stripe_subscription_id="sub_cancel_me"
        )
        dal.commit()

        called: dict[str, Any] = {}

        async def fake_cancel(
            sub_id: str, *, secret_key: str, immediately: bool, client=None
        ) -> dict:
            called["sub_id"] = sub_id
            called["immediately"] = immediately
            return {"id": sub_id, "status": "canceled"}

        monkeypatch.setattr("services.stripe_service.cancel_subscription", fake_cancel)

        response = await client.post(
            "/api/v1/marketplace/premium/cancel",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.premium:admin"),
                "Content-Type": "application/json",
            },
            data=b'{"communityId": %d, "immediately": true}' % community_id,
        )
        assert response.status_code == 200
        assert called["sub_id"] == "sub_cancel_me"
        assert called["immediately"] is True


class TestSubscribePremiumWithStripeMocked:
    async def test_subscribe_calls_stripe_and_returns_checkout_url(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()

        async def fake_checkout(**kwargs: Any) -> dict:
            return {"id": "cs_123", "url": "https://checkout.stripe.com/cs_123"}

        monkeypatch.setattr("services.stripe_service.create_checkout_session", fake_checkout)

        response = await client.post(
            "/api/v1/marketplace/premium/subscribe",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.premium:admin"),
                "Content-Type": "application/json",
            },
            data=(
                b'{"communityId": %d, "provider": "stripe", '
                b'"successUrl": "https://x/ok", "cancelUrl": "https://x/cancel"}' % community_id
            ),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["checkoutUrl"] == "https://checkout.stripe.com/cs_123"
        assert body["sessionId"] == "cs_123"

    async def test_subscribe_stripe_error_returns_502(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()

        from services.errors import ApiError

        async def failing_checkout(**kwargs: Any) -> dict:
            raise ApiError("Stripe down", 502, "STRIPE_ERROR")

        monkeypatch.setattr("services.stripe_service.create_checkout_session", failing_checkout)

        response = await client.post(
            "/api/v1/marketplace/premium/subscribe",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.premium:admin"),
                "Content-Type": "application/json",
            },
            data=(
                b'{"communityId": %d, "provider": "stripe", '
                b'"successUrl": "https://x/ok", "cancelUrl": "https://x/cancel"}' % community_id
            ),
        )
        assert response.status_code == 502


class TestDiscountCodeCrudBlueprint:
    async def test_list_empty(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v", email="v@x.com"))
        dal.commit()
        response = await client.get(
            "/api/v1/marketplace/vendor/discount-codes",
            headers=user_auth_headers(user_id=vendor_id, scope="marketplace.discount:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["discountCodes"] == []

    async def test_create_success(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v2", email="v2@x.com"))
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes",
            headers={
                **user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write"),
                "Content-Type": "application/json",
            },
            data=b'{"code": "NEW10", "discountType": "percentage", "discountValue": 10}',
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["data"]["discountCode"]["code"] == "NEW10"

    async def test_create_conflict_returns_409(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v3", email="v3@x.com"))
        now = dt.datetime.utcnow()
        dal.vendor_discount_codes.insert(
            code="DUPE",
            vendor_id=vendor_id,
            discount_type="percentage",
            discount_value=10,
            current_uses=0,
            valid_from=now,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes",
            headers={
                **user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write"),
                "Content-Type": "application/json",
            },
            data=b'{"code": "DUPE", "discountType": "percentage", "discountValue": 5}',
        )
        assert response.status_code == 409

    async def test_update_success(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v4", email="v4@x.com"))
        now = dt.datetime.utcnow()
        code_id = dal.vendor_discount_codes.insert(
            code="UPDME",
            vendor_id=vendor_id,
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
            headers=user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write"),
            json={"description": "updated"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["discountCode"]["description"] == "updated"

    async def test_update_invalid_discount_value_is_400(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v5", email="v5@x.com"))
        now = dt.datetime.utcnow()
        code_id = dal.vendor_discount_codes.insert(
            code="BADVAL",
            vendor_id=vendor_id,
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
            headers=user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write"),
            json={"discountValue": "not-a-number"},
        )
        assert response.status_code == 400

    async def test_delete_success(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v6", email="v6@x.com"))
        now = dt.datetime.utcnow()
        code_id = dal.vendor_discount_codes.insert(
            code="DELME",
            vendor_id=vendor_id,
            discount_type="percentage",
            discount_value=10,
            current_uses=0,
            valid_from=now,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        dal.commit()
        response = await client.delete(
            f"/api/v1/marketplace/vendor/discount-codes/{code_id}",
            headers=user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["discountCode"]["isActive"] is False

    async def test_validate_endpoint(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="v7", email="v7@x.com"))
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes/validate",
            headers={
                **user_auth_headers(user_id=vendor_id, scope="marketplace.discount:read"),
                "Content-Type": "application/json",
            },
            data=b'{"code": "NOPE"}',
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["valid"] is False
        assert body["reason"] == "CODE_NOT_FOUND"

    async def test_redeem_no_longer_valid_returns_409(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.community_members.insert(
            community_id=community_id, user_id=str(admin_id), role="community-owner", is_active=True
        )
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes/redeem",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.discount:admin"),
                "Content-Type": "application/json",
            },
            data=b'{"codeId": 999999, "communityId": %d, "subscriptionId": 1}' % community_id,
        )
        assert response.status_code == 409


class TestSubscriptionsBlueprint:
    def _module(self, dal: Any) -> int:
        return int(dal.hub_modules.insert(name="weatherbot", is_published=True))

    async def test_list_and_subscribe_and_update_and_unsubscribe(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        module_id = self._module(dal)
        dal.commit()
        # `require_scope`'s wildcard rule matches resource (`*:action`),
        # never action-level hierarchy -- `:admin` does NOT imply `:write`,
        # so read/write routes need their own correctly-scoped headers
        # (this is the correct, intentional behavior; not a workaround).
        headers_read = user_auth_headers(user_id=admin_id, scope="marketplace.subscription:read")
        headers_write = user_auth_headers(user_id=admin_id, scope="marketplace.subscription:write")

        empty = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/subscriptions", headers=headers_read
        )
        assert empty.status_code == 200
        assert (await empty.get_json())["total"] == 0

        subscribe_resp = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/subscriptions",
            headers={**headers_write, "Content-Type": "application/json"},
            data=b'{"moduleId": %d}' % module_id,
        )
        assert subscribe_resp.status_code == 201
        subscription_id = (await subscribe_resp.get_json())["subscription"]["id"]

        update_resp = await client.put(
            f"/api/v1/marketplace/communities/{community_id}/subscriptions/{subscription_id}",
            headers={**headers_write, "Content-Type": "application/json"},
            data=b'{"isEnabled": false}',
        )
        assert update_resp.status_code == 200

        delete_resp = await client.delete(
            f"/api/v1/marketplace/communities/{community_id}/subscriptions/{subscription_id}",
            headers=headers_write,
        )
        assert delete_resp.status_code == 200

    async def test_subscribe_missing_module_is_404(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/subscriptions",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.subscription:write"),
                "Content-Type": "application/json",
            },
            data=b'{"moduleId": 999999}',
        )
        assert response.status_code == 404


class TestPaymentsBlueprint:
    async def test_get_supported_providers(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.get(
            "/api/v1/marketplace/payments/providers",
            headers=user_auth_headers(user_id=admin_id, scope="marketplace.payment:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert "stripe" in body["providers"]

    async def test_checkout_missing_items_is_400(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/payments/checkout",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=b'{"customerEmail": "a@b.com"}',
        )
        assert response.status_code == 400

    async def test_checkout_stripe_success(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()

        async def fake_checkout(**kwargs: Any) -> dict:
            return {"id": "cs_x", "url": "https://checkout.stripe.com/cs_x"}

        monkeypatch.setattr("services.stripe_service.create_checkout_session", fake_checkout)

        response = await client.post(
            "/api/v1/marketplace/payments/checkout",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=(
                b'{"provider": "stripe", "customerEmail": "a@b.com", '
                b'"items": [{"name": "Widget", "price": 5.0, "quantity": 1}]}'
            ),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["sessionId"] == "cs_x"

    async def test_cancel_payment_subscription_unsupported_provider(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/payments/subscriptions/paypal/sub_1/cancel",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=b"{}",
        )
        assert response.status_code == 400

    async def test_cancel_payment_subscription_stripe_success(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()

        async def fake_cancel(
            sub_id: str, *, secret_key: str, immediately: bool, client=None
        ) -> dict:
            return {"id": sub_id, "status": "canceled", "cancel_at_period_end": False}

        monkeypatch.setattr("services.stripe_service.cancel_subscription", fake_cancel)

        response = await client.post(
            "/api/v1/marketplace/payments/subscriptions/stripe/sub_1/cancel",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=b'{"immediately": true}',
        )
        assert response.status_code == 200

    async def test_refund_missing_fields_is_400(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/payments/refunds",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=b"{}",
        )
        assert response.status_code == 400

    async def test_refund_owner_succeeds(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        owner_id = int(dal.hub_users.insert(username="owner", email="owner@x.com"))
        dal.commit()

        async def fake_get_session(session_id: str, *, secret_key: str, client=None) -> dict:
            return {"metadata": {"userId": str(owner_id)}, "payment_intent": "pi_owned"}

        async def fake_refund(**kwargs: Any) -> dict:
            return {"id": "re_1", "status": "succeeded"}

        monkeypatch.setattr("services.stripe_service.get_checkout_session", fake_get_session)
        monkeypatch.setattr("services.stripe_service.create_refund", fake_refund)

        response = await client.post(
            "/api/v1/marketplace/payments/refunds",
            headers={
                **user_auth_headers(user_id=owner_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=b'{"provider": "stripe", "paymentId": "cs_owned"}',
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["refundId"] == "re_1"

    async def test_refund_non_owner_non_admin_is_403(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        owner_id = int(dal.hub_users.insert(username="owner2", email="owner2@x.com"))
        attacker_id = int(dal.hub_users.insert(username="attacker2", email="attacker2@x.com"))
        dal.commit()

        async def fake_get_session(session_id: str, *, secret_key: str, client=None) -> dict:
            return {"metadata": {"userId": str(owner_id)}, "payment_intent": "pi_owned"}

        monkeypatch.setattr("services.stripe_service.get_checkout_session", fake_get_session)

        response = await client.post(
            "/api/v1/marketplace/payments/refunds",
            headers={
                **user_auth_headers(user_id=attacker_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=b'{"provider": "stripe", "paymentId": "cs_owned"}',
        )
        assert response.status_code == 403
