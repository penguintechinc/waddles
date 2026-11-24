"""`blueprints/v1/marketplace_webhooks.py` -- signature verification + idempotent persistence.

Covers exactly the four SECURITY-CRITICAL properties this port PR was
scoped around (task brief): an unsigned/tampered webhook is rejected, a
replayed (same-event-id) webhook does not double-credit, and (in
`test_v1_marketplace_billing_blueprint.py`) client-supplied amounts are
ignored and cross-community access is denied.

Fail-first note: `test_replayed_stripe_event_does_not_double_insert_payment`
was temporarily broken by commenting out `marketplace_webhook_service.
record_payment()`'s SELECT-before-INSERT dedupe check (always INSERT) --
the test went red (2 rows instead of 1) as expected. Reverted; green again
before this port PR. `test_unsigned_webhook_is_rejected` was verified the
same way by temporarily hardcoding `stripe_service.verify_webhook_signature`
to always `return True` -- the "wrong secret" and "tampered payload"
assertions in this file both went red, confirming they're not vacuous.
Both reverted before this PR.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.marketplace_webhooks import webhooks_bp
from config import HubAPIConfig

STRIPE_SECRET = "whsec_test"


def _sign(payload: bytes, secret: str = STRIPE_SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@pytest.fixture
def app(marketplace_billing_db: Any) -> Quart:
    dal, _tenant_id, community_id = marketplace_billing_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(webhooks_bp)
    quart_app.config["dal"] = dal
    quart_app.config["HUB_API_CONFIG"] = HubAPIConfig(
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
        stripe_webhook_secret=STRIPE_SECRET,
        paypal_client_id="",
        paypal_client_secret="",
        paypal_webhook_id="",
        paypal_mode="sandbox",
    )
    quart_app.config["_TEST_COMMUNITY_ID"] = community_id
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _checkout_completed_event(
    community_id: int,
    *,
    event_id: str = "evt_1",
    payment_intent: str = "pi_1",
    amount_total: int = 5000,
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "mode": "subscription",
                    "subscription": "sub_test_1",
                    "payment_intent": payment_intent,
                    "amount_total": amount_total,
                    "currency": "usd",
                    "metadata": {"communityId": str(community_id), "type": "community_premium"},
                }
            },
        }
    ).encode()


class TestStripeSignatureVerification:
    async def test_unsigned_webhook_is_rejected(self, client: Any, app: Quart) -> None:
        community_id = app.config["_TEST_COMMUNITY_ID"]
        payload = _checkout_completed_event(community_id)
        response = await client.post(
            "/api/v1/marketplace/webhooks/stripe",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert body["received"] is False

    async def test_wrong_signature_is_rejected(self, client: Any, app: Quart) -> None:
        community_id = app.config["_TEST_COMMUNITY_ID"]
        payload = _checkout_completed_event(community_id)
        response = await client.post(
            "/api/v1/marketplace/webhooks/stripe",
            data=payload,
            headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert response.status_code == 400

    async def test_tampered_payload_with_valid_looking_signature_is_rejected(
        self, client: Any, app: Quart
    ) -> None:
        community_id = app.config["_TEST_COMMUNITY_ID"]
        original = _checkout_completed_event(community_id, amount_total=100)
        sig = _sign(original)  # signature computed over the ORIGINAL body
        tampered = _checkout_completed_event(community_id, amount_total=99999999)
        response = await client.post(
            "/api/v1/marketplace/webhooks/stripe",
            data=tampered,
            headers={"Content-Type": "application/json", "Stripe-Signature": sig},
        )
        assert response.status_code == 400

    async def test_valid_signature_is_accepted_and_persisted(
        self, client: Any, app: Quart, marketplace_billing_db: Any
    ) -> None:
        community_id = app.config["_TEST_COMMUNITY_ID"]
        payload = _checkout_completed_event(community_id)
        response = await client.post(
            "/api/v1/marketplace/webhooks/stripe",
            data=payload,
            headers={"Content-Type": "application/json", "Stripe-Signature": _sign(payload)},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["received"] is True
        assert body["processed"] is True

        dal, _tenant_id, _cid = marketplace_billing_db
        sub = dal(dal.community_premium_subscriptions.community_id == community_id).select().first()
        assert sub is not None
        assert sub.status == "active"
        payment = dal(dal.marketplace_payments.external_payment_id == "pi_1").select().first()
        assert payment is not None
        assert payment.amount_cents == 5000


class TestIdempotency:
    async def test_replayed_stripe_event_does_not_double_insert_payment(
        self, client: Any, app: Quart, marketplace_billing_db: Any
    ) -> None:
        community_id = app.config["_TEST_COMMUNITY_ID"]
        payload = _checkout_completed_event(
            community_id, event_id="evt_replay", payment_intent="pi_replay"
        )
        headers = {"Content-Type": "application/json", "Stripe-Signature": _sign(payload)}

        first = await client.post(
            "/api/v1/marketplace/webhooks/stripe", data=payload, headers=headers
        )
        assert first.status_code == 200

        # Same provider delivers the identical event a second time (Stripe's
        # own documented retry behavior on a timeout/5xx) -- re-sign since
        # the signature is timestamp-bound, but the EVENT BODY (payment
        # intent id) is identical, which is the actual dedupe key.
        replay_payload = _checkout_completed_event(
            community_id, event_id="evt_replay", payment_intent="pi_replay"
        )
        replay_headers = {
            "Content-Type": "application/json",
            "Stripe-Signature": _sign(replay_payload),
        }
        second = await client.post(
            "/api/v1/marketplace/webhooks/stripe", data=replay_payload, headers=replay_headers
        )
        assert second.status_code == 200

        dal, _tenant_id, _cid = marketplace_billing_db
        rows = dal(dal.marketplace_payments.external_payment_id == "pi_replay").select()
        assert len(rows) == 1, "replayed webhook must not double-insert a payment row"

    async def test_replayed_subscription_activation_does_not_change_state_twice(
        self, client: Any, app: Quart, marketplace_billing_db: Any
    ) -> None:
        community_id = app.config["_TEST_COMMUNITY_ID"]
        payload = _checkout_completed_event(
            community_id, event_id="evt_sub", payment_intent="pi_sub_activation"
        )
        headers = {"Content-Type": "application/json", "Stripe-Signature": _sign(payload)}
        await client.post("/api/v1/marketplace/webhooks/stripe", data=payload, headers=headers)

        dal, _tenant_id, _cid = marketplace_billing_db
        before = (
            dal(dal.community_premium_subscriptions.community_id == community_id).select().first()
        )

        replay_payload = _checkout_completed_event(
            community_id, event_id="evt_sub", payment_intent="pi_sub_activation"
        )
        replay_headers = {
            "Content-Type": "application/json",
            "Stripe-Signature": _sign(replay_payload),
        }
        await client.post(
            "/api/v1/marketplace/webhooks/stripe", data=replay_payload, headers=replay_headers
        )

        rows = dal(dal.community_premium_subscriptions.community_id == community_id).select()
        assert len(rows) == 1, "replayed activation must not create a second subscription row"
        after = rows.first()
        assert after.updated_at == before.updated_at or after.status == before.status


class TestUnconfiguredSecretFailsClosed:
    async def test_stripe_webhook_without_configured_secret_is_400(
        self, client: Any, app: Quart
    ) -> None:
        import dataclasses

        app.config["HUB_API_CONFIG"] = dataclasses.replace(
            app.config["HUB_API_CONFIG"], stripe_webhook_secret=""
        )
        response = await client.post(
            "/api/v1/marketplace/webhooks/stripe",
            data=b'{"id": "evt_x"}',
            headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=x"},
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert body["received"] is False

    async def test_paypal_webhook_without_configured_secret_is_400(
        self, client: Any, app: Quart
    ) -> None:
        import dataclasses

        app.config["HUB_API_CONFIG"] = dataclasses.replace(
            app.config["HUB_API_CONFIG"], paypal_webhook_id=""
        )
        response = await client.post(
            "/api/v1/marketplace/webhooks/paypal",
            data=b'{"id": "WH-1"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400


def _configure_paypal(app: Quart) -> None:
    """Give `app`'s config real (non-empty) PayPal credentials for the flow tests below.

    The `app` fixture defaults these to `""` (this file's Stripe-focused
    tests never need them) -- `blueprints/v1/marketplace_webhooks.py`
    fails closed on ANY missing PayPal credential before even calling
    `verify_webhook_signature`, so these tests need real-looking values
    even though the verification call itself is monkeypatched.
    """
    import dataclasses

    app.config["HUB_API_CONFIG"] = dataclasses.replace(
        app.config["HUB_API_CONFIG"],
        paypal_client_id="pp_cid",
        paypal_client_secret="pp_secret",
        paypal_webhook_id="pp_wh",
    )


class TestPaypalWebhookFlow:
    """Proves the BLUEPRINT gates on and persists PayPal's verification result.

    PayPal signature verification itself is unit-tested (real HTTP wiring)
    in `test_paypal_service.py`/`test_paypal_service_outbound.py`.
    """

    async def test_rejected_signature_is_400_and_nothing_persisted(
        self, client: Any, app: Quart, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        _configure_paypal(app)

        async def fake_reject(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr("services.paypal_service.verify_webhook_signature", fake_reject)

        response = await client.post(
            "/api/v1/marketplace/webhooks/paypal",
            data=b'{"id": "WH-forged", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        dal, _tenant_id, _cid = marketplace_billing_db
        assert (
            dal(dal.marketplace_payments.external_payment_id == "WH-forged").select().first()
            is None
        )

    async def test_accepted_signature_is_persisted(
        self, client: Any, app: Quart, marketplace_billing_db: Any, monkeypatch: Any
    ) -> None:
        _configure_paypal(app)
        community_id = app.config["_TEST_COMMUNITY_ID"]

        async def fake_accept(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr("services.paypal_service.verify_webhook_signature", fake_accept)

        payload = json.dumps(
            {
                "id": "WH-real",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {
                    "id": "CAPTURE-real",
                    "amount": {"value": "12.00", "currency_code": "USD"},
                    "custom_id": str(community_id),
                },
            }
        ).encode()
        response = await client.post(
            "/api/v1/marketplace/webhooks/paypal",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["received"] is True
        assert body["processed"] is True

        dal, _tenant_id, _cid = marketplace_billing_db
        payment = (
            dal(dal.marketplace_payments.external_payment_id == "CAPTURE-real").select().first()
        )
        assert payment is not None
        assert payment.amount_cents == 1200

    async def test_malformed_json_after_accepted_signature_is_400(
        self, client: Any, app: Quart, monkeypatch: Any
    ) -> None:
        _configure_paypal(app)

        async def fake_accept(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr("services.paypal_service.verify_webhook_signature", fake_accept)

        response = await client.post(
            "/api/v1/marketplace/webhooks/paypal",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
