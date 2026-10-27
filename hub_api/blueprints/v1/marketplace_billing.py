"""v1 `marketplace.billing` group -- subscriptions/payments/premium/discount-codes (M4).

Ports `subscriptionController.js` (community module installs),
`paymentController.js` (generic Stripe/PayPal checkout orchestration),
`premiumController.js` (community-wide seat-based premium), and
`discountCodeController.js` (vendor discount codes) from
`admin/marketplace_module/backend/src`. Webhook RECEIVERS live in the
sibling `marketplace_webhooks.py` (provider callbacks, no tenant/JWT --
different auth model entirely, see that module's docstring).

Mount prefix (`/api/v1/marketplace/...`) matches the pinned frontend
contract for the two groups `admin/hub_module/frontend/src/services/
api.js` already calls (`marketplacePremiumApi` -> `/api/v1/marketplace/
premium/*`, `marketplaceVendorApi` -> `/api/v1/marketplace/vendor/*`) --
per the migration plan's D6 decision ("marketplace = module within
hub-api, not a container"), every other endpoint in this group (community
module subscriptions, generic payments) that api.js does not yet call
extends the SAME `/api/v1/marketplace/...` namespace rather than Node's
own bare `/api/v1/...` mount (that mount only existed because Node ran
marketplace_module as a separate microservice behind a gateway that
itself added the `/marketplace` prefix -- see `hub_api/PORTING.md`-style
provenance note, not a literal Node route to copy byte-for-byte since no
gateway exists in this consolidated app).

**Auth deviations from Node (security fixes, not faithful ports)**:
1. `paymentController.js`/`routes/payments.js` has NO auth middleware on
   ANY route except `/refunds` (`requireAuth` there only) -- unauthenticated
   checkout-session creation, subscription cancel/reactivate BY ID, and
   customer/payment-method enumeration were all reachable with no token.
   Every route in `payments_bp` below requires `tenant_middleware` +
   `require_scope` (task instructions: "extra care", "don't faithfully
   port the vuln").
2. `subscriptionController.js`'s Node routes were community-admin-gated
   already (`requireCommunityAdmin`) -- reproduced here via
   `services.marketplace_billing_service.is_community_admin()`, since a
   scope bundle alone (`marketplace.subscription:*`) proves role, not
   "administers THIS community" (IDOR -- see that function's docstring).
3. `premiumController.js`'s `getPricing` (no `communityId` query param)
   stays genuinely public/pre-auth, matching Node's own route (no
   `requireAuth` at all) -- a static pricing-config read, not a per-
   community resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import marketplace_billing_service as svc
from services import paypal_service, stripe_service
from services.community_common import api_error, community_in_tenant
from services.current_user import get_current_user_id
from services.errors import ApiError

premium_bp = Blueprint("v1_marketplace_premium", __name__, url_prefix="/api/v1/marketplace/premium")
discount_bp = Blueprint(
    "v1_marketplace_discount_codes",
    __name__,
    url_prefix="/api/v1/marketplace/vendor/discount-codes",
)
subscriptions_bp = Blueprint(
    "v1_marketplace_subscriptions", __name__, url_prefix="/api/v1/marketplace/communities"
)
payments_bp = Blueprint(
    "v1_marketplace_payments", __name__, url_prefix="/api/v1/marketplace/payments"
)


def _dal() -> Any:
    return current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return api_error(exc.message, exc.status_code)


def _caller_has_scope(required_scope: str) -> bool:
    """Independently re-check the bearer token's `scope` claim for `required_scope`.

    Used for a resource-level admin OVERRIDE (refund owner-or-admin) on top
    of the route's own `require_scope` gate -- checks the OIDC scope claim
    only (security.md: never branch on role names), same wildcard rule
    `flask_core.authz._scope_covers` documents (`*:action` covers any
    resource for that action), re-derived locally rather than reaching into
    `flask_core.authz`'s private helpers.
    """
    from flask_core.auth import verify_jwt_token
    from flask_core.secrets import require_secret_key

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    decoded = verify_jwt_token(auth_header[7:], require_secret_key())
    if not decoded:
        return False
    granted = str(decoded.get("scope", "")).split()
    required_resource, _, required_action = required_scope.partition(":")
    for scope in granted:
        if scope == required_scope:
            return True
        resource, _, action = scope.partition(":")
        if resource == "*" and action == required_action:
            return True
    return False


def _require_community_admin(community_id: int) -> tuple[dict[str, object], int] | None:
    """Tenant-isolation + per-community-admin gate. Error tuple to short-circuit, or `None`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran
    dal = _dal()
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", 404)
    user_id = get_current_user_id(request)
    if not svc.is_community_admin(dal, community_id, user_id):
        return api_error("Community admin access required", 403)
    return None


# ---------------------------------------------------------------------------
# Premium
# ---------------------------------------------------------------------------


@premium_bp.route("/pricing", methods=["GET"])
@validate_response(svc.PricingConfigDTO)
async def get_pricing() -> svc.PricingConfigDTO:
    """`GET /pricing` -- public (matches Node: no `requireAuth`)."""
    dal = _dal()
    base_price, base_seat_limit, overage_price = svc.get_pricing_config(dal)
    community_id_raw = request.args.get("communityId")
    if community_id_raw is None:
        return svc.PricingConfigDTO(
            basePriceCents=base_price,
            baseSeatLimit=base_seat_limit,
            overagePriceCents=overage_price,
        )
    try:
        community_id = int(community_id_raw)
    except ValueError:
        return svc.PricingConfigDTO(
            basePriceCents=base_price,
            baseSeatLimit=base_seat_limit,
            overagePriceCents=overage_price,
        )
    seat_count = svc.get_current_seat_count(dal, community_id)
    estimated = svc.calculate_monthly_bill(seat_count, base_price, base_seat_limit, overage_price)
    return svc.PricingConfigDTO(
        basePriceCents=base_price,
        baseSeatLimit=base_seat_limit,
        overagePriceCents=overage_price,
        seatCount=seat_count,
        estimatedCents=estimated,
    )


@premium_bp.route("/status/<int:community_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.premium:admin")  # type: ignore[misc]
async def get_premium_status(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /status/<communityId>` -- community-admin only (IDOR-checked)."""
    denied = _require_community_admin(community_id)
    if denied is not None:
        return denied
    dal = _dal()
    status = svc.get_premium_status(dal, community_id)
    seat_count = svc.get_current_seat_count(dal, community_id)
    return {
        "success": True,
        "subscription": _dto_or_none(status),
        "currentSeatCount": seat_count,
    }, 200


def _dto_or_none(dto: Any) -> dict[str, Any] | None:
    if dto is None:
        return None
    import dataclasses

    return dataclasses.asdict(dto)


@dataclass(slots=True, frozen=True)
class PremiumSubscribeRequest:
    """Request DTO for `POST /subscribe` -- caller picks a plan, NEVER a price."""

    communityId: int
    provider: str = "stripe"
    successUrl: str = ""
    cancelUrl: str = ""


@premium_bp.route("/subscribe", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.premium:admin")  # type: ignore[misc]
@validate_request(PremiumSubscribeRequest)
async def subscribe_premium(data: PremiumSubscribeRequest) -> tuple[dict[str, object], int]:
    """`POST /subscribe` -- price is SERVER-computed (see `calculate_monthly_bill`'s docstring)."""
    denied = _require_community_admin(data.communityId)
    if denied is not None:
        return denied
    dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101

    base_price, base_seat_limit, overage_price = svc.get_pricing_config(dal)
    seat_count = svc.get_current_seat_count(dal, data.communityId)
    total_cents = svc.calculate_monthly_bill(seat_count, base_price, base_seat_limit, overage_price)

    checkout_url: str | None = None
    session_id: str | None = None
    if data.provider == "stripe":
        cfg = current_app.config["HUB_API_CONFIG"]
        try:
            result = await stripe_service.create_checkout_session(
                secret_key=cfg.stripe_secret_key,
                items=[
                    {
                        "name": "WaddleBot Community Premium",
                        "description": f"{seat_count} seats",
                        "price": total_cents / 100,
                        "currency": "usd",
                        "quantity": 1,
                    }
                ],
                customer_email=None,
                success_url=data.successUrl,
                cancel_url=data.cancelUrl,
                metadata={"communityId": str(data.communityId), "type": "community_premium"},
            )
        except (ApiError, httpx.HTTPError) as exc:
            return api_error(f"Failed to create checkout session: {exc}", 502)
        checkout_url = result.get("url")
        session_id = result.get("id")

    svc.upsert_trialing_premium_subscription(
        dal, data.communityId, ctx.tenant_id, base_price, overage_price, base_seat_limit, seat_count
    )

    return {
        "success": True,
        "checkoutUrl": checkout_url,
        "sessionId": session_id,
        "pricing": {
            "basePriceCents": base_price,
            "seatCount": seat_count,
            "totalCents": total_cents,
        },
    }, 200


@dataclass(slots=True, frozen=True)
class PremiumCancelRequest:
    """Request DTO for `POST /cancel`."""

    communityId: int
    immediately: bool = False


@premium_bp.route("/cancel", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.premium:admin")  # type: ignore[misc]
@validate_request(PremiumCancelRequest)
async def cancel_premium(data: PremiumCancelRequest) -> tuple[dict[str, object], int]:
    """`POST /cancel`."""
    denied = _require_community_admin(data.communityId)
    if denied is not None:
        return denied
    dal = _dal()
    try:
        updated = svc.cancel_premium_subscription(dal, data.communityId, data.immediately)
    except ApiError as exc:
        return _err(exc)

    if data.immediately and updated.stripeSubscriptionId:
        cfg = current_app.config["HUB_API_CONFIG"]
        try:
            await stripe_service.cancel_subscription(
                updated.stripeSubscriptionId, secret_key=cfg.stripe_secret_key, immediately=True
            )
        except (ApiError, httpx.HTTPError):
            pass  # local state already updated; provider-side cleanup best-effort here

    return {"success": True, "cancelAtPeriodEnd": not data.immediately}, 200


# ---------------------------------------------------------------------------
# Vendor discount codes
# ---------------------------------------------------------------------------


@discount_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.discount:read")  # type: ignore[misc]
async def list_discount_codes() -> tuple[dict[str, object], int]:
    """`GET /vendor/discount-codes` -- codes owned by the CALLER (vendor self-service)."""
    vendor_id = get_current_user_id(request)
    page = max(1, _int_arg("page", 1))
    limit = min(100, max(1, _int_arg("limit", 20)))
    status = request.args.get("status", "all")
    if status not in ("active", "expired", "all"):
        status = "all"
    result = svc.list_vendor_discount_codes(
        _dal(), vendor_id, page=page, limit=limit, status=status
    )
    import dataclasses

    return {"success": True, "data": dataclasses.asdict(result)}, 200


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, str(default)))
    except ValueError:
        return default


@dataclass(slots=True, frozen=True)
class CreateDiscountCodeRequest:
    """Request DTO for `POST /vendor/discount-codes`."""

    code: str
    discountType: str
    discountValue: float
    moduleId: int | None = None
    validFrom: str | None = None
    validUntil: str | None = None
    maxUses: int | None = None
    description: str | None = None


@discount_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.discount:write")  # type: ignore[misc]
@validate_request(CreateDiscountCodeRequest)
async def create_discount_code(data: CreateDiscountCodeRequest) -> tuple[dict[str, object], int]:
    """`POST /vendor/discount-codes`."""
    import dataclasses
    from datetime import datetime

    vendor_id = get_current_user_id(request)
    try:
        dto = svc.create_discount_code(
            _dal(),
            vendor_id,
            code=data.code,
            discount_type=data.discountType,
            discount_value=data.discountValue,
            module_id=data.moduleId,
            valid_from=datetime.fromisoformat(data.validFrom) if data.validFrom else None,
            valid_until=datetime.fromisoformat(data.validUntil) if data.validUntil else None,
            max_uses=data.maxUses,
            description=data.description,
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "data": {"discountCode": dataclasses.asdict(dto)}}, 201


@discount_bp.route("/<int:code_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.discount:write")  # type: ignore[misc]
async def update_discount_code(code_id: int) -> tuple[dict[str, object], int]:
    """`PUT /vendor/discount-codes/<id>` -- ownership-checked (IDOR-safe)."""
    import dataclasses

    vendor_id = get_current_user_id(request)
    payload = await request.get_json(force=True, silent=True) or {}
    if "discountValue" in payload:
        try:
            payload["discountValue"] = float(payload["discountValue"])
        except (TypeError, ValueError):
            return api_error("discountValue must be a positive number", 400)
    try:
        dto = svc.update_discount_code(_dal(), vendor_id, code_id, payload)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "data": {"discountCode": dataclasses.asdict(dto)}}, 200


@discount_bp.route("/<int:code_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.discount:write")  # type: ignore[misc]
async def delete_discount_code(code_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /vendor/discount-codes/<id>` -- soft delete, ownership-checked."""
    import dataclasses

    vendor_id = get_current_user_id(request)
    try:
        dto = svc.delete_discount_code(_dal(), vendor_id, code_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "data": {"discountCode": dataclasses.asdict(dto)}}, 200


@dataclass(slots=True, frozen=True)
class ValidateDiscountCodeRequest:
    """Request DTO for `POST /vendor/discount-codes/validate`."""

    code: str
    moduleId: int | None = None


@discount_bp.route("/validate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.discount:read")  # type: ignore[misc]
@validate_request(ValidateDiscountCodeRequest)
@validate_response(svc.DiscountValidationDTO)
async def validate_discount_code(
    data: ValidateDiscountCodeRequest,
) -> svc.DiscountValidationDTO | tuple[dict[str, object], int]:
    """`POST /vendor/discount-codes/validate` -- any authenticated caller (Node's `requireAuth`)."""
    try:
        return svc.validate_discount_code(_dal(), data.code, data.moduleId)
    except ApiError as exc:
        return _err(exc)


@dataclass(slots=True, frozen=True)
class RedeemDiscountCodeRequest:
    """Request DTO for `POST /vendor/discount-codes/redeem`.

    Deliberately carries NO price field -- Node's `originalPriceCents` is
    dropped (see `services.marketplace_billing_service.redeem_discount_code`'s
    docstring: the price is recomputed server-side, never trusted from the
    caller).
    """

    codeId: int
    communityId: int
    subscriptionId: int


@discount_bp.route("/redeem", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.discount:admin")  # type: ignore[misc]
@validate_request(RedeemDiscountCodeRequest)
async def redeem_discount_code(data: RedeemDiscountCodeRequest) -> tuple[dict[str, object], int]:
    """`POST /vendor/discount-codes/redeem` -- community-admin-gated + server-priced (IDOR fix)."""
    denied = _require_community_admin(data.communityId)
    if denied is not None:
        return denied
    try:
        result = svc.redeem_discount_code(
            _dal(), data.codeId, data.communityId, data.subscriptionId
        )
    except ApiError as exc:
        return _err(exc)
    import dataclasses

    return {"success": True, "data": dataclasses.asdict(result)}, 200


# ---------------------------------------------------------------------------
# Community module subscriptions (hub_module_installations)
# ---------------------------------------------------------------------------


@subscriptions_bp.route("/<int:community_id>/subscriptions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.subscription:read")  # type: ignore[misc]
async def list_subscriptions(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /communities/<id>/subscriptions`."""
    denied = _require_community_admin(community_id)
    if denied is not None:
        return denied
    import dataclasses

    rows = svc.list_community_subscriptions(_dal(), community_id)
    return {
        "success": True,
        "subscriptions": [dataclasses.asdict(r) for r in rows],
        "total": len(rows),
    }, 200


@dataclass(slots=True, frozen=True)
class SubscribeModuleRequest:
    """Request DTO for `POST /communities/<id>/subscriptions`."""

    moduleId: int


@subscriptions_bp.route("/<int:community_id>/subscriptions", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.subscription:write")  # type: ignore[misc]
@validate_request(SubscribeModuleRequest)
async def subscribe_module(
    community_id: int, data: SubscribeModuleRequest
) -> tuple[dict[str, object], int]:
    """`POST /communities/<id>/subscriptions` -- install a published module."""
    denied = _require_community_admin(community_id)
    if denied is not None:
        return denied
    user_id = get_current_user_id(request)
    try:
        new_id = svc.subscribe_module(_dal(), community_id, data.moduleId, user_id)
    except ApiError as exc:
        return _err(exc)
    return {
        "success": True,
        "message": "Module subscribed successfully",
        "subscription": {"id": new_id},
    }, 201


@subscriptions_bp.route("/<int:community_id>/subscriptions/<int:subscription_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.subscription:write")  # type: ignore[misc]
async def update_subscription(
    community_id: int, subscription_id: int
) -> tuple[dict[str, object], int]:
    """`PUT /communities/<id>/subscriptions/<subscriptionId>`."""
    denied = _require_community_admin(community_id)
    if denied is not None:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    try:
        svc.update_module_subscription(
            _dal(),
            community_id,
            subscription_id,
            config=payload.get("config"),
            is_enabled=payload.get("isEnabled"),
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Subscription updated successfully"}, 200


@subscriptions_bp.route(
    "/<int:community_id>/subscriptions/<int:subscription_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.subscription:write")  # type: ignore[misc]
async def unsubscribe_module(
    community_id: int, subscription_id: int
) -> tuple[dict[str, object], int]:
    """`DELETE /communities/<id>/subscriptions/<subscriptionId>`."""
    denied = _require_community_admin(community_id)
    if denied is not None:
        return denied
    try:
        svc.unsubscribe_module(_dal(), community_id, subscription_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Module unsubscribed successfully"}, 200


# ---------------------------------------------------------------------------
# Generic payments (Stripe/PayPal checkout orchestration)
# ---------------------------------------------------------------------------


def _provider_secret_key(provider: str) -> str:
    cfg = current_app.config["HUB_API_CONFIG"]
    if provider == "stripe":
        return cast(str, cfg.stripe_secret_key)
    raise ApiError(f"Unsupported payment provider: {provider}", 400, "BAD_REQUEST")


@payments_bp.route("/checkout", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.payment:write")  # type: ignore[misc]
async def create_checkout() -> tuple[dict[str, object], int]:
    """`POST /payments/checkout`."""
    payload = await request.get_json(force=True, silent=True) or {}
    items = payload.get("items")
    customer_email = payload.get("customerEmail")
    provider = payload.get("provider", "stripe")
    if not items or not isinstance(items, list):
        return api_error("Items array is required", 400)
    if not customer_email:
        return api_error("Customer email is required", 400)

    user_id = get_current_user_id(request)
    metadata = {**(payload.get("metadata") or {}), "userId": str(user_id)}
    cfg = current_app.config["HUB_API_CONFIG"]
    try:
        if provider == "stripe":
            result = await stripe_service.create_checkout_session(
                secret_key=cfg.stripe_secret_key,
                items=items,
                customer_email=customer_email,
                success_url=payload.get("successUrl", ""),
                cancel_url=payload.get("cancelUrl", ""),
                metadata=metadata,
            )
            return {"success": True, "sessionId": result.get("id"), "url": result.get("url")}, 200
        if provider == "paypal":
            total = sum(float(i["price"]) * i.get("quantity", 1) for i in items)
            result = await paypal_service.create_order(
                client_id=cfg.paypal_client_id,
                client_secret=cfg.paypal_client_secret,
                mode=cfg.paypal_mode,
                items=items,
                total_amount=total,
                currency=items[0].get("currency", "USD"),
                return_url=payload.get("successUrl", ""),
                cancel_url=payload.get("cancelUrl", ""),
                metadata=metadata,
            )
            approval = next(
                (link["href"] for link in result.get("links", []) if link.get("rel") == "approve"),
                None,
            )
            return {"success": True, "orderId": result.get("id"), "url": approval}, 200
    except (ApiError, httpx.HTTPError) as exc:
        return api_error(f"Checkout creation failed: {exc}", 502)
    return api_error(f"Unsupported payment provider: {provider}", 400)


@payments_bp.route("/subscriptions/<provider>/<sub_id>/cancel", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.payment:write")  # type: ignore[misc]
async def cancel_payment_subscription(provider: str, sub_id: str) -> tuple[dict[str, object], int]:
    """`POST /payments/subscriptions/<provider>/<id>/cancel`."""
    payload = await request.get_json(force=True, silent=True) or {}
    immediately = bool(payload.get("immediately", False))
    if provider != "stripe":
        return api_error(f"Subscription cancellation not implemented for provider: {provider}", 400)
    try:
        cfg = current_app.config["HUB_API_CONFIG"]
        result = await stripe_service.cancel_subscription(
            sub_id, secret_key=cfg.stripe_secret_key, immediately=immediately
        )
    except (ApiError, httpx.HTTPError) as exc:
        return api_error(f"Subscription cancellation failed: {exc}", 502)
    return {
        "success": True,
        "subscriptionId": result.get("id"),
        "status": result.get("status"),
        "cancelAtPeriodEnd": result.get("cancel_at_period_end"),
    }, 200


@payments_bp.route("/refunds", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.payment:write")  # type: ignore[misc]
async def create_refund() -> tuple[dict[str, object], int]:
    """`POST /payments/refunds` -- admin OR the original payment's owner only, never any caller."""
    payload = await request.get_json(force=True, silent=True) or {}
    provider = payload.get("provider")
    payment_id = payload.get("paymentId")
    if not provider or not payment_id:
        return api_error("Provider and payment ID are required", 400)

    user_id = get_current_user_id(request)
    cfg = current_app.config["HUB_API_CONFIG"]
    try:
        if provider == "stripe":
            session = await stripe_service.get_checkout_session(
                payment_id, secret_key=cfg.stripe_secret_key
            )
            owner_id = (session.get("metadata") or {}).get("userId")
            payment_intent = session.get("payment_intent")
        else:
            return api_error(f"Refund resolution not implemented for provider: {provider}", 400)
    except (ApiError, httpx.HTTPError) as exc:
        return api_error(f"Could not resolve payment: {exc}", 404)

    is_admin = _caller_has_scope("marketplace.payment:admin")
    is_owner = bool(owner_id) and str(owner_id) == str(user_id)
    if not is_admin and not is_owner:
        return api_error("Not authorized to refund this payment", 403)

    if not payment_intent:
        return api_error("Payment has not been captured and cannot be refunded", 400)

    try:
        refund = await stripe_service.create_refund(
            secret_key=cfg.stripe_secret_key,
            payment_intent_id=payment_intent,
            amount_cents=round(float(payload["amount"]) * 100) if payload.get("amount") else None,
            reason=payload.get("reason", "requested_by_customer"),
            metadata={"refundedBy": str(user_id)},
        )
    except (ApiError, httpx.HTTPError) as exc:
        return api_error(f"Refund creation failed: {exc}", 502)
    return {"success": True, "refundId": refund.get("id"), "status": refund.get("status")}, 200


@payments_bp.route("/providers", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace.payment:read")  # type: ignore[misc]
async def get_supported_providers() -> tuple[dict[str, object], int]:
    """`GET /payments/providers`."""
    return {"success": True, "providers": ["stripe", "paypal"], "defaultProvider": "stripe"}, 200


BLUEPRINTS: list[Blueprint] = [premium_bp, discount_bp, subscriptions_bp, payments_bp]
