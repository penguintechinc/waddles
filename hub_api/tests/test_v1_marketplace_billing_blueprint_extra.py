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
import re
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
from tests.community_ids import install_community_ids, public_id
from tests.conftest import insert_global_admin

UNKNOWN_UUID = "00000000-0000-4000-8000-00000000dead"


def _code_url(dal: Any, code_id: int) -> str:
    """Discount-code route addressed by the code's public UUID."""
    row_uuid = _row_uuid(dal, "vendor_discount_codes", code_id)
    return f"/api/v1/marketplace/vendor/discount-codes/{row_uuid}"


def _row_uuid(dal: Any, table: str, row_id: int) -> str:
    """`public_id` of a seeded row (entity routes take it, never the numeric id)."""
    row = dal(dal[table].id == row_id).select(dal[table].public_id).first()
    assert row is not None and row.public_id
    return str(row.public_id)


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
    install_community_ids(quart_app, dal)
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
            f"/api/v1/marketplace/premium/status/{public_id(dal, community_id)}",
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
            data=b'{"communityId": "%s", "immediately": false}'
            % public_id(dal, community_id).encode(),
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
            data=b'{"communityId": "%s", "immediately": true}'
            % public_id(dal, community_id).encode(),
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
                b'{"communityId": "%s", "provider": "stripe", '
                b'"successUrl": "https://x/ok", "cancelUrl": "https://x/cancel"}'
                % public_id(dal, community_id).encode()
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
                b'{"communityId": "%s", "provider": "stripe", '
                b'"successUrl": "https://x/ok", "cancelUrl": "https://x/cancel"}'
                % public_id(dal, community_id).encode()
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
            _code_url(dal, code_id),
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
            _code_url(dal, code_id),
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
            _code_url(dal, code_id),
            headers=user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["discountCode"]["isActive"] is False

    async def test_validate_endpoint(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        from services.schema import bind_marketplace_vendor_tables

        bind_marketplace_vendor_tables(dal, migrate=True)
        vendor_id = int(dal.hub_users.insert(username="v7", email="v7@x.com"))
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes/validate",
            headers={
                **user_auth_headers(user_id=vendor_id, scope="marketplace.discount:read"),
                "Content-Type": "application/json",
            },
            data=b'{"code": "NOPE", "moduleId": "%s"}' % UNKNOWN_UUID.encode(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["valid"] is False
        assert body.get("reason") is None
        assert body.get("vendorId") is None

    async def test_redeem_no_longer_valid_returns_409(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        dal.community_members.insert(
            community_id=community_id, user_id=str(admin_id), is_active=True
        )
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/vendor/discount-codes/redeem",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.discount:admin"),
                "Content-Type": "application/json",
            },
            data=b'{"codeId": "%s", "communityId": "%s", "subscriptionId": 1}'
            % (UNKNOWN_UUID.encode(), public_id(dal, community_id).encode()),
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
            f"/api/v1/marketplace/communities/{public_id(dal, community_id)}/subscriptions",
            headers=headers_read,
        )
        assert empty.status_code == 200
        assert (await empty.get_json())["total"] == 0

        subscribe_resp = await client.post(
            f"/api/v1/marketplace/communities/{public_id(dal, community_id)}/subscriptions",
            headers={**headers_write, "Content-Type": "application/json"},
            data=b'{"moduleId": "%s"}' % _row_uuid(dal, "hub_modules", module_id).encode(),
        )
        assert subscribe_resp.status_code == 201
        subscription_id = (await subscribe_resp.get_json())["subscription"]["id"]
        # The handle is the module's public UUID (numeric installation ids never cross the API).
        assert subscription_id == _row_uuid(dal, "hub_modules", module_id)

        update_resp = await client.put(
            f"/api/v1/marketplace/communities/{public_id(dal, community_id)}"
            f"/subscriptions/{subscription_id}",
            headers={**headers_write, "Content-Type": "application/json"},
            data=b'{"isEnabled": false}',
        )
        assert update_resp.status_code == 200

        delete_resp = await client.delete(
            f"/api/v1/marketplace/communities/{public_id(dal, community_id)}"
            f"/subscriptions/{subscription_id}",
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
            f"/api/v1/marketplace/communities/{public_id(dal, community_id)}/subscriptions",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.subscription:write"),
                "Content-Type": "application/json",
            },
            data=b'{"moduleId": "%s"}' % UNKNOWN_UUID.encode(),
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

    async def test_checkout_stripe_response_omits_the_paypal_field(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        """Stripe variant returns `sessionId`+`url` only -- never a paypal `orderId`.

        `/payments/checkout` has no `@validate_response` DTO; its response keys
        are picked per `provider` (stripe -> `sessionId`, paypal -> `orderId`).
        Pin the stripe variant's exact field set so a future edit can't let a
        paypal-only field leak onto the stripe branch.
        """
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()

        async def fake_checkout(**kwargs: Any) -> dict:
            return {"id": "cs_shape", "url": "https://checkout.stripe.com/cs_shape"}

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
        assert body["sessionId"] == "cs_shape"
        assert body["url"] == "https://checkout.stripe.com/cs_shape"
        assert "orderId" not in body

    async def test_checkout_paypal_returns_order_id_not_session_id(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        """PayPal variant returns `orderId`+`url` only -- never a stripe `sessionId`.

        The sibling of the stripe test: the paypal branch builds a different
        response shape (order id + the `approve`-rel approval link). Proves the
        paypal variant is exercised end to end and does not carry the stripe
        field.
        """
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()

        async def fake_order(**kwargs: Any) -> dict:
            return {
                "id": "pp_order_1",
                "links": [
                    {"rel": "self", "href": "https://api.paypal.com/v2/checkout/orders/pp_order_1"},
                    {
                        "rel": "approve",
                        "href": "https://www.paypal.com/checkoutnow?token=pp_order_1",
                    },
                ],
            }

        monkeypatch.setattr("services.paypal_service.create_order", fake_order)

        response = await client.post(
            "/api/v1/marketplace/payments/checkout",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=(
                b'{"provider": "paypal", "customerEmail": "a@b.com", '
                b'"items": [{"name": "Widget", "price": 5.0, "quantity": 1, "currency": "USD"}]}'
            ),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["orderId"] == "pp_order_1"
        assert body["url"] == "https://www.paypal.com/checkoutnow?token=pp_order_1"
        assert "sessionId" not in body

    async def test_checkout_unknown_provider_is_400(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        """Reject-unknown-discriminator: a provider outside {stripe, paypal} is a 400.

        The discriminator must not silently default to stripe or fall through
        to a 200 with an empty body -- an unrecognized `provider` is a clean
        client error with no provider call made.
        """
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/payments/checkout",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.payment:write"),
                "Content-Type": "application/json",
            },
            data=(
                b'{"provider": "venmo", "customerEmail": "a@b.com", '
                b'"items": [{"name": "Widget", "price": 5.0, "quantity": 1}]}'
            ),
        )
        assert response.status_code == 400

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


class TestPremiumRequestCommunityIsPublicUuid:
    """`communityId` request fields take the community UUID; numeric / unknown are refused."""

    async def test_numeric_community_id_rejected_by_validation(
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
        assert response.status_code in (400, 422)

    async def test_unknown_uuid_is_fenced_like_an_unknown_community(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        admin_id = _admin(dal)
        dal.commit()
        response = await client.post(
            "/api/v1/marketplace/premium/cancel",
            headers={
                **user_auth_headers(user_id=admin_id, scope="marketplace.premium:admin"),
                "Content-Type": "application/json",
            },
            data=b'{"communityId": "00000000-0000-4000-8000-00000000dead", "immediately": false}',
        )
        assert response.status_code in (403, 404)

    async def test_pricing_query_takes_uuid_and_ignores_numeric(
        self, client: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        by_uuid = await client.get(
            f"/api/v1/marketplace/premium/pricing?communityId={public_id(dal, community_id)}"
        )
        assert by_uuid.status_code == 200
        assert (await by_uuid.get_json())["seatCount"] is not None
        numeric = await client.get(
            f"/api/v1/marketplace/premium/pricing?communityId={community_id}"
        )
        assert numeric.status_code == 200
        assert (await numeric.get_json())["seatCount"] is None


_UUID_SHAPE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def _seed_code(dal: Any, vendor_id: int, code: str) -> int:
    now = dt.datetime.utcnow()
    row_id = dal.vendor_discount_codes.insert(
        code=code,
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
    return int(row_id)


class TestPublicUuidRoutes:
    """Group C: discount-code and subscription ids cross the API as public UUIDs only."""

    async def test_uuid_resolves_and_response_emits_uuid(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        vendor_id = int(dal.hub_users.insert(username="u1", email="u1@x.com"))
        code_id = _seed_code(dal, vendor_id, "UUIDOK")
        code_uuid = _row_uuid(dal, "vendor_discount_codes", code_id)
        headers = user_auth_headers(user_id=vendor_id, scope="marketplace.discount:write")
        response = await client.put(
            f"/api/v1/marketplace/vendor/discount-codes/{code_uuid}",
            headers=headers,
            json={"description": "via uuid"},
        )
        assert response.status_code == 200
        dto = (await response.get_json())["data"]["discountCode"]
        assert dto["id"] == code_uuid and _UUID_SHAPE.match(dto["id"])
        listing = await client.get(
            "/api/v1/marketplace/vendor/discount-codes",
            headers=user_auth_headers(user_id=vendor_id, scope="marketplace.discount:read"),
        )
        codes = (await listing.get_json())["data"]["discountCodes"]
        assert [c["id"] for c in codes] == [code_uuid]  # denominator: one code examined

    async def test_foreign_unknown_malformed_and_numeric_ids_are_the_same_404(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, _c = marketplace_billing_db
        owner = int(dal.hub_users.insert(username="o1", email="o1@x.com"))
        other = int(dal.hub_users.insert(username="o2", email="o2@x.com"))
        code_id = _seed_code(dal, owner, "FOREIGN")
        headers = user_auth_headers(user_id=other, scope="marketplace.discount:write")
        bodies = []
        for segment in (
            _row_uuid(dal, "vendor_discount_codes", code_id),  # foreign vendor's real code
            UNKNOWN_UUID,
            "not-a-uuid",
            str(code_id),  # the old numeric form no longer routes
        ):
            response = await client.delete(
                f"/api/v1/marketplace/vendor/discount-codes/{segment}", headers=headers
            )
            assert response.status_code == 404, segment
            bodies.append(await response.get_json())
        # the owner's code is untouched (still active)
        assert dal.vendor_discount_codes[code_id].is_active is True
        assert bodies[0]["error"]["message"] == bodies[1]["error"]["message"]  # no oracle

    async def test_subscription_list_emits_uuid_and_numeric_path_404s(
        self, client: Any, user_auth_headers: Any, marketplace_billing_db: Any
    ) -> None:
        dal, _t, community_id = marketplace_billing_db
        admin_id = _admin(dal)
        module_id = int(dal.hub_modules.insert(name="uuidmod", is_published=True))
        dal.commit()
        community = public_id(dal, community_id)
        write = user_auth_headers(user_id=admin_id, scope="marketplace.subscription:write")
        read = user_auth_headers(user_id=admin_id, scope="marketplace.subscription:read")
        module_uuid = _row_uuid(dal, "hub_modules", module_id)
        base = f"/api/v1/marketplace/communities/{community}/subscriptions"
        created = await client.post(
            base,
            headers={**write, "Content-Type": "application/json"},
            data=b'{"moduleId": "%s"}' % module_uuid.encode(),
        )
        assert created.status_code == 201
        listed = (await (await client.get(base, headers=read)).get_json())["subscriptions"]
        assert [(r["id"], r["moduleId"]) for r in listed] == [(module_uuid, module_uuid)]
        for segment in (str(module_id), "nope", UNKNOWN_UUID):
            response = await client.put(
                f"{base}/{segment}",
                headers={**write, "Content-Type": "application/json"},
                data=b'{"isEnabled": false}',
            )
            assert response.status_code == 404, segment
        # a module NOT installed in this community is also just "not found" here
        other_module = int(dal.hub_modules.insert(name="othermod", is_published=True))
        dal.commit()
        response = await client.delete(
            f"{base}/{_row_uuid(dal, 'hub_modules', other_module)}", headers=write
        )
        assert response.status_code == 404
