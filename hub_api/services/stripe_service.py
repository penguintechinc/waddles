"""Stripe REST client -- port of `stripeService.js` via `httpx` (no `stripe` SDK dependency).

`requirements.in` already pins `httpx` for the proxy clients/Bot
module outbound calls; this module reuses that same dependency rather than
adding the `stripe` PyPI package (per this port's instruction: use an
already-pinned dependency, don't add a new one without calling it out --
none added here).

**Webhook signature verification is the security-critical half of this
module** (the token-billing spec that scoped this port flagged Node's
webhook handlers as stubs that never persist -- see `hub_api/PORTING.md`'s
sibling `paypal_service.py` for the other provider). `verify_webhook_
signature()`/`parse_webhook_event()` reimplement Stripe's documented HMAC
scheme (https://docs.stripe.com/webhooks#verify-manually) directly against
`hmac`/`hashlib` (stdlib, no new dependency): parse `t=<timestamp>,
v1=<sig>[,v1=<sig>...]` from `Stripe-Signature`, recompute
`HMAC-SHA256(webhook_secret, f"{t}.{raw_body}")`, and compare via
`hmac.compare_digest` against every `v1` value present (Stripe rotates
signing secrets by sending multiple `v1` values during rotation) -- this is
the FULL algorithm Stripe's own SDK runs, not a stub. A timestamp older
than `tolerance_seconds` (default 300s, Stripe's own documented default)
is rejected even with a valid signature -- defense in depth against a
captured-and-replayed request on top of `marketplace_webhook_service.py`'s
own idempotency-by-event-id check.

Outbound calls (checkout/subscription/refund/customer) are real REST calls
to `https://api.stripe.com/v1/*` (HTTP Basic auth, secret key as username,
Stripe's own convention) -- not stubbed, since Stripe's REST API needs no
SDK-specific behavior `httpx` can't express.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from services.errors import ApiError, bad_request

STRIPE_API_BASE = "https://api.stripe.com/v1"


def verify_webhook_signature(
    payload: bytes,
    sig_header: str | None,
    webhook_secret: str,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify a `Stripe-Signature` header against `payload` using `webhook_secret`.

    Fails closed: a missing header, missing secret, malformed header, no
    matching `v1` signature, or a timestamp outside `tolerance_seconds`
    all return `False` -- never raises, so callers can't accidentally treat
    an exception path as "verified" by forgetting a `try`/`except`.
    """
    if not sig_header or not webhook_secret:
        return False

    timestamp: str | None = None
    signatures: list[str] = []
    for part in sig_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        return False

    try:
        ts_int = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - ts_int) > tolerance_seconds:
        return False

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()

    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def parse_webhook_event(
    payload: bytes,
    sig_header: str | None,
    webhook_secret: str,
    *,
    tolerance_seconds: int = 300,
) -> dict[str, Any]:
    """Verify then parse a Stripe webhook body; raises `ApiError(400)` on any failure.

    Equivalent to `stripeService.js::constructWebhookEvent` -- the point at
    which an unverified/tampered payload is rejected before any handler
    ever sees it.
    """
    if not verify_webhook_signature(
        payload, sig_header, webhook_secret, tolerance_seconds=tolerance_seconds
    ):
        raise bad_request("Webhook signature verification failed")
    try:
        event: dict[str, Any] = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise bad_request("Webhook payload is not valid JSON") from exc
    return event


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested dict/list into Stripe's `a[b][c]=value` form-encoding shape."""
    flat: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        composed = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, composed))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                item_prefix = f"{composed}[{idx}]"
                if isinstance(item, dict):
                    flat.update(_flatten(item, item_prefix))
                else:
                    flat[item_prefix] = str(item)
        elif isinstance(value, bool):
            flat[composed] = "true" if value else "false"
        else:
            flat[composed] = str(value)
    return flat


async def _request(
    method: str,
    path: str,
    secret_key: str,
    *,
    data: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST/GET to the Stripe REST API with HTTP Basic auth; raises `ApiError` on failure."""
    form = _flatten(data or {})
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await http_client.request(
            method,
            f"{STRIPE_API_BASE}{path}",
            data=form if method != "GET" else None,
            params=form if method == "GET" else None,
            auth=(secret_key, ""),
        )
    finally:
        if owns_client:
            await http_client.aclose()

    body: dict[str, Any] = response.json()
    if response.status_code >= 400:
        message = body.get("error", {}).get("message", "Stripe API error")
        raise ApiError(message, response.status_code, "STRIPE_ERROR")
    return body


async def create_checkout_session(
    *,
    secret_key: str,
    items: list[dict[str, Any]],
    customer_email: str | None,
    success_url: str,
    cancel_url: str,
    metadata: dict[str, str] | None = None,
    mode: str = "payment",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session -- one-time payment (`mode="payment"`)."""
    line_items = [
        {
            "price_data": {
                "currency": item.get("currency", "usd"),
                "product_data": {"name": item["name"], "description": item.get("description")},
                "unit_amount": round(float(item["price"]) * 100),
            },
            "quantity": item.get("quantity", 1),
        }
        for item in items
    ]
    payload = {
        "payment_method_types": ["card"],
        "line_items": line_items,
        "mode": mode,
        "customer_email": customer_email,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {**(metadata or {}), "source": "waddlebot_marketplace"},
    }
    return await _request("POST", "/checkout/sessions", secret_key, data=payload, client=client)


async def create_subscription_session(
    *,
    secret_key: str,
    price_id: str,
    customer_email: str | None,
    success_url: str,
    cancel_url: str,
    trial_period_days: int = 0,
    metadata: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for a subscription (`mode="subscription"`)."""
    subscription_data: dict[str, Any] = {"metadata": metadata or {}}
    if trial_period_days > 0:
        subscription_data["trial_period_days"] = trial_period_days
    payload = {
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "customer_email": customer_email,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {**(metadata or {}), "source": "waddlebot_marketplace"},
        "subscription_data": subscription_data,
    }
    return await _request("POST", "/checkout/sessions", secret_key, data=payload, client=client)


async def get_checkout_session(
    session_id: str, *, secret_key: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Retrieve a Checkout Session."""
    return await _request("GET", f"/checkout/sessions/{session_id}", secret_key, client=client)


async def get_subscription(
    subscription_id: str, *, secret_key: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Retrieve a subscription."""
    return await _request("GET", f"/subscriptions/{subscription_id}", secret_key, client=client)


async def cancel_subscription(
    subscription_id: str,
    *,
    secret_key: str,
    immediately: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Cancel (immediately) or schedule-cancel-at-period-end a subscription."""
    if immediately:
        return await _request(
            "DELETE", f"/subscriptions/{subscription_id}", secret_key, client=client
        )
    return await _request(
        "POST",
        f"/subscriptions/{subscription_id}",
        secret_key,
        data={"cancel_at_period_end": True},
        client=client,
    )


async def reactivate_subscription(
    subscription_id: str, *, secret_key: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Undo a scheduled cancel-at-period-end."""
    return await _request(
        "POST",
        f"/subscriptions/{subscription_id}",
        secret_key,
        data={"cancel_at_period_end": False},
        client=client,
    )


async def create_refund(
    *,
    secret_key: str,
    payment_intent_id: str,
    amount_cents: int | None = None,
    reason: str = "requested_by_customer",
    metadata: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Create a refund against a PaymentIntent."""
    payload: dict[str, Any] = {
        "payment_intent": payment_intent_id,
        "reason": reason,
        "metadata": {**(metadata or {}), "source": "waddlebot_marketplace"},
    }
    if amount_cents is not None:
        payload["amount"] = amount_cents
    return await _request("POST", "/refunds", secret_key, data=payload, client=client)


async def get_refund(
    refund_id: str, *, secret_key: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Retrieve a refund."""
    return await _request("GET", f"/refunds/{refund_id}", secret_key, client=client)


async def create_customer(
    *,
    secret_key: str,
    email: str,
    name: str,
    metadata: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Create a Stripe customer."""
    payload = {
        "email": email,
        "name": name,
        "metadata": {**(metadata or {}), "source": "waddlebot_marketplace"},
    }
    return await _request("POST", "/customers", secret_key, data=payload, client=client)


async def get_customer(
    customer_id: str, *, secret_key: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Retrieve a customer."""
    return await _request("GET", f"/customers/{customer_id}", secret_key, client=client)


async def list_payment_methods(
    customer_id: str, *, secret_key: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """List a customer's card payment methods."""
    return await _request(
        "GET",
        "/payment_methods",
        secret_key,
        data={"customer": customer_id, "type": "card"},
        client=client,
    )
