"""Business logic for the M4 Marketplace Billing group.

Ports `subscriptionController.js` (community module installs),
`premiumController.js` (community premium seat-based subscriptions), and
`discountCodeController.js` (vendor discount codes) onto the REAL schema
(see `services/schema.py::bind_marketplace_billing_tables`'s docstring for
the `vendor_discount_codes`/`discount_code_redemptions` table-name fix).
`paymentController.js`'s generic checkout/refund/customer orchestration
lives directly in `blueprints/v1/marketplace_billing.py` (thin glue over
`services/stripe_service.py`/`services/paypal_service.py`, no owned
tables of its own beyond `marketplace_payments`, written by
`services/marketplace_webhook_service.py`).

Query style matches `services/community_common.py`'s established
Community-module (M6) convention -- the raw `pydal` `dal` (never
`AsyncDAL`), synchronous pydal query builder calls from inside async
handlers, explicit `dal.commit()` after every write. This sidesteps
`hub_api/PORTING.md` Gotchas #1-#3 entirely (those are specific to
`AsyncDAL.insert_async()`'s executor-thread/nested-dataclass-response
interaction) rather than needing the `jsonify_dto()` workaround M1 uses.

**IDOR fix** (task security mandate, not a faithful Node port): Node's
`requireCommunityAdmin` middleware checks `community_members.role` for the
SPECIFIC `communityId` in the request, in addition to a super-admin/
platform-admin bypass -- `is_community_admin()` below reproduces that
per-community check. Scope checks alone (`require_scope("marketplace.
subscription:admin")`) prove the caller holds a role BUNDLE, not that they
administer THIS community; without this second gate, any tenant-scoped
admin-bundle caller could manage ANY other community's subscription/
premium/discount-code state in the same tenant.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from services.errors import bad_request, conflict, forbidden, not_found
from services.global_identity import is_global_admin_sync

#: Community roles Node's `requireCommunityAdmin` treats as admin-equivalent.
_COMMUNITY_ADMIN_ROLES = ("community-owner", "community-admin", "moderator")

_VALID_DISCOUNT_TYPES = ("percentage", "fixed_amount", "free")


def is_community_admin(dal: Any, community_id: int, user_id: int) -> bool:
    """True iff `user_id` may administer `community_id`'s billing state.

    Super admins bypass (matches Node's `req.user.isSuperAdmin` check);
    everyone else needs an active `community_members` row for THIS
    community with an admin-equivalent role. Returns `False` (never
    raises) on any lookup miss -- callers convert that into a 403.
    """
    if is_global_admin_sync(dal, user_id):
        return True

    member = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
        )
        .select(dal.community_members.role)
        .first()
    )
    return member is not None and member.role in _COMMUNITY_ADMIN_ROLES


# ---------------------------------------------------------------------------
# Premium (community-wide, seat-based) subscriptions
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PricingConfigDTO:
    """`marketplace_settings`-backed premium pricing configuration."""

    basePriceCents: int
    baseSeatLimit: int
    overagePriceCents: int
    seatCount: int | None = None
    estimatedCents: int | None = None


def get_pricing_config(dal: Any) -> tuple[int, int, int]:
    """Read premium pricing (base/seat_limit/overage cents) from `marketplace_settings`."""
    rows = dal(
        dal.marketplace_settings.setting_key.belongs(
            (
                "community_premium_base_price_cents",
                "community_premium_base_seat_limit",
                "community_premium_overage_price_cents",
            )
        )
    ).select()
    config = {row.setting_key: row.setting_value for row in rows}
    return (
        int(config.get("community_premium_base_price_cents") or 500),
        int(config.get("community_premium_base_seat_limit") or 50),
        int(config.get("community_premium_overage_price_cents") or 10),
    )


def get_current_seat_count(dal: Any, community_id: int) -> int:
    """Count active `community_members` rows for `community_id`."""
    return int(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
        ).count()
    )


def calculate_monthly_bill(
    seat_count: int, base_price_cents: int, base_seat_limit: int, overage_price_cents: int
) -> int:
    """Base price + per-seat overage beyond `base_seat_limit` -- SERVER-computed, never client-set.

    Security note (task mandate): the caller only ever supplies
    `communityId`/`provider`/`successUrl`/`cancelUrl` to `/premium/subscribe`
    -- there is no client-writable amount field anywhere in this chain.
    Every dollar figure charged is derived here, from `marketplace_settings`
    and the live `community_members` count, matching Node's own
    `calculateMonthlyBill` (this is one Node behavior that was already
    correct -- preserved, not "fixed").
    """
    return base_price_cents + max(0, seat_count - base_seat_limit) * overage_price_cents


@dataclass(slots=True, frozen=True)
class PremiumStatusDTO:
    """Response shape for `GET /premium/status/<communityId>`."""

    communityId: int
    status: str
    stripeSubscriptionId: str | None
    paypalSubscriptionId: str | None
    currentSeatCount: int
    basePriceCents: int
    overagePriceCents: int
    baseSeatLimit: int
    cancelAtPeriodEnd: bool
    currentPeriodEnd: str | None


def _iso(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return None


def get_premium_status(dal: Any, community_id: int) -> PremiumStatusDTO | None:
    """`community_premium_subscriptions` row for `community_id`, or `None` if never subscribed."""
    row = dal(dal.community_premium_subscriptions.community_id == community_id).select().first()
    if row is None:
        return None
    return PremiumStatusDTO(
        communityId=community_id,
        status=row.status,
        stripeSubscriptionId=row.stripe_subscription_id,
        paypalSubscriptionId=row.paypal_subscription_id,
        currentSeatCount=row.current_seat_count or 0,
        basePriceCents=row.base_price_cents,
        overagePriceCents=row.overage_price_cents,
        baseSeatLimit=row.base_seat_limit,
        cancelAtPeriodEnd=bool(row.cancel_at_period_end),
        currentPeriodEnd=_iso(row.current_period_end),
    )


def upsert_trialing_premium_subscription(
    dal: Any,
    community_id: int,
    tenant_id: int,
    base_price_cents: int,
    overage_price_cents: int,
    base_seat_limit: int,
    seat_count: int,
) -> None:
    """Insert-or-update `community_premium_subscriptions` into the `trialing` state.

    Native upsert on the table's own `UNIQUE(community_id)` constraint --
    mirrors Node's `ON CONFLICT (community_id) DO UPDATE`.
    """
    existing = (
        dal(dal.community_premium_subscriptions.community_id == community_id).select().first()
    )
    now = dt.datetime.utcnow()
    if existing is None:
        dal.community_premium_subscriptions.insert(
            community_id=community_id,
            tenant_id=tenant_id,
            status="trialing",
            base_price_cents=base_price_cents,
            overage_price_cents=overage_price_cents,
            base_seat_limit=base_seat_limit,
            current_seat_count=seat_count,
            created_at=now,
            updated_at=now,
        )
    else:
        dal(dal.community_premium_subscriptions.id == existing.id).update(
            status="trialing", current_seat_count=seat_count, updated_at=now
        )
    dal.commit()


def cancel_premium_subscription(dal: Any, community_id: int, immediately: bool) -> PremiumStatusDTO:
    """Mark a `community_premium_subscriptions` row canceled; raises `ApiError(404)` if missing."""
    row = dal(dal.community_premium_subscriptions.community_id == community_id).select().first()
    if row is None:
        raise not_found("No premium subscription found")

    dal(dal.community_premium_subscriptions.id == row.id).update(
        status="canceled", cancel_at_period_end=True, updated_at=dt.datetime.utcnow()
    )
    dal.commit()

    updated = get_premium_status(dal, community_id)
    assert updated is not None  # nosec B101 - row just updated in the same transaction
    return updated


# ---------------------------------------------------------------------------
# Community module subscriptions (`hub_module_installations`)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ModuleSubscriptionDTO:
    """One row of `GET /communities/<id>/subscriptions`."""

    id: int
    moduleId: int
    name: str
    displayName: str
    description: str | None
    version: str | None
    author: str | None
    category: str | None
    iconUrl: str | None
    isCore: bool
    isEnabled: bool
    installedAt: str | None
    updatedAt: str | None


def list_community_subscriptions(dal: Any, community_id: int) -> list[ModuleSubscriptionDTO]:
    """`GET /communities/<id>/subscriptions` -- installed modules, core-first."""
    rows = dal(
        (dal.hub_module_installations.community_id == community_id)
        & (dal.hub_module_installations.module_id == dal.hub_modules.id)
    ).select(
        dal.hub_module_installations.ALL,
        dal.hub_modules.ALL,
        orderby=(~dal.hub_modules.is_core, ~dal.hub_module_installations.installed_at),
    )
    result = []
    for row in rows:
        inst = row.hub_module_installations
        mod = row.hub_modules
        result.append(
            ModuleSubscriptionDTO(
                id=inst.id,
                moduleId=mod.id,
                name=mod.name,
                displayName=mod.display_name or mod.name,
                description=mod.description,
                version=mod.version,
                author=mod.author,
                category=mod.category,
                iconUrl=mod.icon_url,
                isCore=bool(mod.is_core),
                isEnabled=bool(inst.is_enabled),
                installedAt=_iso(inst.installed_at),
                updatedAt=_iso(inst.updated_at),
            )
        )
    return result


def subscribe_module(dal: Any, community_id: int, module_id: int, installed_by: int) -> int:
    """Install a published module into a community; returns the new installation id."""
    module_row = (
        dal(
            (dal.hub_modules.id == module_id) & (dal.hub_modules.is_published == True)  # noqa: E712 - pydal Field comparison
        )
        .select()
        .first()
    )
    if module_row is None:
        raise not_found("Module not found")

    existing = (
        dal(
            (dal.hub_module_installations.community_id == community_id)
            & (dal.hub_module_installations.module_id == module_id)
        )
        .select()
        .first()
    )
    if existing is not None:
        raise conflict("Module already installed")

    now = dt.datetime.utcnow()
    new_id = dal.hub_module_installations.insert(
        community_id=community_id,
        module_id=module_id,
        installed_by=installed_by,
        is_enabled=True,
        installed_at=now,
        updated_at=now,
    )
    dal.commit()
    return int(new_id)


def unsubscribe_module(dal: Any, community_id: int, subscription_id: int) -> None:
    """Uninstall a module; raises `ApiError(404)`/`ApiError(400)` matching Node's checks."""
    row = (
        dal(
            (dal.hub_module_installations.id == subscription_id)
            & (dal.hub_module_installations.community_id == community_id)
            & (dal.hub_module_installations.module_id == dal.hub_modules.id)
        )
        .select(dal.hub_modules.is_core)
        .first()
    )
    if row is None:
        raise not_found("Subscription not found")
    if bool(row.is_core):
        raise bad_request("Cannot unsubscribe from core modules")

    dal(
        (dal.hub_module_installations.id == subscription_id)
        & (dal.hub_module_installations.community_id == community_id)
    ).delete()
    dal.commit()


def update_module_subscription(
    dal: Any,
    community_id: int,
    subscription_id: int,
    *,
    config: dict[str, Any] | None,
    is_enabled: bool | None,
) -> None:
    """Update `config`/`is_enabled` on an installation; raises `ApiError(400)`/`ApiError(404)`."""
    if config is None and is_enabled is None:
        raise bad_request("No configuration provided")

    updates: dict[str, Any] = {"updated_at": dt.datetime.utcnow()}
    if config is not None:
        updates["config"] = config
    if is_enabled is not None:
        updates["is_enabled"] = is_enabled

    affected = dal(
        (dal.hub_module_installations.id == subscription_id)
        & (dal.hub_module_installations.community_id == community_id)
    ).update(**updates)
    dal.commit()
    if not affected:
        raise not_found("Subscription not found")


# ---------------------------------------------------------------------------
# Vendor discount codes
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DiscountCodeDTO:
    """Wire shape for a `vendor_discount_codes` row."""

    id: int
    code: str
    vendorId: int
    moduleId: int | None
    discountType: str
    discountValue: float
    maxUses: int | None
    currentUses: int
    validFrom: str | None
    validUntil: str | None
    isActive: bool
    description: str | None
    createdAt: str | None
    updatedAt: str | None


def _to_dto(row: Any) -> DiscountCodeDTO:
    value = row.discount_value
    return DiscountCodeDTO(
        id=row.id,
        code=row.code,
        vendorId=row.vendor_id,
        moduleId=row.module_id,
        discountType=row.discount_type,
        discountValue=float(value) if isinstance(value, Decimal) else float(value or 0),
        maxUses=row.max_uses,
        currentUses=row.current_uses,
        validFrom=_iso(row.valid_from),
        validUntil=_iso(row.valid_until),
        isActive=bool(row.is_active),
        description=row.description,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


@dataclass(slots=True, frozen=True)
class DiscountCodePageDTO:
    """Paginated discount-code list."""

    discountCodes: list[DiscountCodeDTO]
    page: int
    limit: int
    total: int


def list_vendor_discount_codes(
    dal: Any, vendor_id: int, *, page: int = 1, limit: int = 20, status: str = "all"
) -> DiscountCodePageDTO:
    """`GET /vendor/discount-codes` -- codes owned by `vendor_id`, optionally filtered by status."""
    query = dal.vendor_discount_codes.vendor_id == vendor_id
    now = dt.datetime.utcnow()
    if status == "active":
        query &= dal.vendor_discount_codes.is_active == True  # noqa: E712
        query &= (dal.vendor_discount_codes.valid_until == None) | (  # noqa: E711
            dal.vendor_discount_codes.valid_until >= now
        )
    elif status == "expired":
        query &= (dal.vendor_discount_codes.is_active == False) | (  # noqa: E712
            (dal.vendor_discount_codes.valid_until != None)  # noqa: E711
            & (dal.vendor_discount_codes.valid_until < now)
        )

    total = dal(query).count()
    rows = dal(query).select(
        orderby=~dal.vendor_discount_codes.created_at, limitby=((page - 1) * limit, page * limit)
    )
    return DiscountCodePageDTO(
        discountCodes=[_to_dto(row) for row in rows], page=page, limit=limit, total=int(total)
    )


def _validate_discount_fields(discount_type: str, discount_value: float) -> None:
    if discount_type not in _VALID_DISCOUNT_TYPES:
        raise bad_request(f"discountType must be one of: {', '.join(_VALID_DISCOUNT_TYPES)}")
    if discount_type != "free" and discount_value <= 0:
        raise bad_request("discountValue must be a positive number")
    if discount_type == "percentage" and discount_value > 100:
        raise bad_request("discountValue cannot exceed 100 for percentage discounts")


def create_discount_code(
    dal: Any,
    vendor_id: int,
    *,
    code: str,
    discount_type: str,
    discount_value: float,
    module_id: int | None,
    valid_from: dt.datetime | None,
    valid_until: dt.datetime | None,
    max_uses: int | None,
    description: str | None,
) -> DiscountCodeDTO:
    """Create a vendor-owned discount code; case-insensitively unique per vendor."""
    if not code or not code.strip():
        raise bad_request("code is required")
    _validate_discount_fields(discount_type, discount_value)

    upper_code = code.strip().upper()
    dupe = (
        dal(
            (dal.vendor_discount_codes.vendor_id == vendor_id)
            & (dal.vendor_discount_codes.code.upper() == upper_code)
        )
        .select()
        .first()
    )
    if dupe is not None:
        raise conflict("A discount code with that value already exists for this vendor")

    now = dt.datetime.utcnow()
    new_id = dal.vendor_discount_codes.insert(
        code=upper_code,
        vendor_id=vendor_id,
        module_id=module_id,
        discount_type=discount_type,
        discount_value=discount_value,
        max_uses=max_uses,
        current_uses=0,
        valid_from=valid_from or now,
        valid_until=valid_until,
        is_active=True,
        description=description,
        created_at=now,
        updated_at=now,
    )
    dal.commit()
    row = dal(dal.vendor_discount_codes.id == new_id).select().first()
    return _to_dto(row)


def _owned_code(dal: Any, vendor_id: int, code_id: int) -> Any:
    row = dal(dal.vendor_discount_codes.id == code_id).select().first()
    if row is None:
        raise not_found("Discount code not found")
    if row.vendor_id != vendor_id:
        raise forbidden("You do not have permission to modify this discount code")
    return row


_UPDATE_FIELD_MAP = {
    "discountType": "discount_type",
    "discountValue": "discount_value",
    "validFrom": "valid_from",
    "validUntil": "valid_until",
    "maxUses": "max_uses",
    "isActive": "is_active",
    "description": "description",
    "moduleId": "module_id",
}


def update_discount_code(
    dal: Any, vendor_id: int, code_id: int, updates: dict[str, Any]
) -> DiscountCodeDTO:
    """Partially update a vendor-owned discount code (ownership-checked -- IDOR-safe)."""
    existing = _owned_code(dal, vendor_id, code_id)

    set_clauses: dict[str, Any] = {}
    for camel, column in _UPDATE_FIELD_MAP.items():
        if camel in updates:
            set_clauses[column] = updates[camel]

    new_type = set_clauses.get("discount_type", existing.discount_type)
    new_value = set_clauses.get("discount_value", existing.discount_value)
    _validate_discount_fields(new_type, float(new_value))

    if not set_clauses:
        return _to_dto(existing)

    set_clauses["updated_at"] = dt.datetime.utcnow()
    dal(dal.vendor_discount_codes.id == code_id).update(**set_clauses)
    dal.commit()
    row = dal(dal.vendor_discount_codes.id == code_id).select().first()
    return _to_dto(row)


def delete_discount_code(dal: Any, vendor_id: int, code_id: int) -> DiscountCodeDTO:
    """Soft-delete (deactivate) a vendor-owned discount code."""
    _owned_code(dal, vendor_id, code_id)
    dal(dal.vendor_discount_codes.id == code_id).update(
        is_active=False, updated_at=dt.datetime.utcnow()
    )
    dal.commit()
    row = dal(dal.vendor_discount_codes.id == code_id).select().first()
    return _to_dto(row)


@dataclass(slots=True, frozen=True)
class DiscountValidationDTO:
    """Response for `POST /vendor/discount-codes/validate`."""

    valid: bool
    reason: str | None = None
    discountCodeId: int | None = None
    discountType: str | None = None
    discountValue: float | None = None
    vendorId: int | None = None
    moduleId: int | None = None
    usesRemaining: int | None = None


def validate_discount_code(dal: Any, code: str, module_id: int | None) -> DiscountValidationDTO:
    """Public-shape validity check -- active, in-window, under `max_uses`, module-scoped if set."""
    if not code:
        raise bad_request("code is required")

    row = dal(dal.vendor_discount_codes.code == code.strip().upper()).select().first()
    if row is None:
        return DiscountValidationDTO(valid=False, reason="CODE_NOT_FOUND")
    if not row.is_active:
        return DiscountValidationDTO(valid=False, reason="CODE_INACTIVE")
    now = dt.datetime.utcnow()
    if row.valid_from and row.valid_from > now:
        return DiscountValidationDTO(valid=False, reason="CODE_NOT_YET_VALID")
    if row.valid_until and row.valid_until < now:
        return DiscountValidationDTO(valid=False, reason="CODE_EXPIRED")
    if row.max_uses is not None and row.current_uses >= row.max_uses:
        return DiscountValidationDTO(valid=False, reason="CODE_MAX_USES_REACHED")
    if row.module_id is not None and module_id is not None and row.module_id != module_id:
        return DiscountValidationDTO(valid=False, reason="CODE_WRONG_MODULE")

    return DiscountValidationDTO(
        valid=True,
        discountCodeId=row.id,
        discountType=row.discount_type,
        discountValue=float(row.discount_value),
        vendorId=row.vendor_id,
        moduleId=row.module_id,
        usesRemaining=(row.max_uses - row.current_uses) if row.max_uses is not None else None,
    )


@dataclass(slots=True, frozen=True)
class DiscountRedemptionDTO:
    """Response for `POST /vendor/discount-codes/redeem`."""

    redemptionId: int
    originalPriceCents: int
    discountedPriceCents: int
    discountAmountCents: int


def redeem_discount_code(
    dal: Any, code_id: int, community_id: int, subscription_id: int
) -> DiscountRedemptionDTO:
    """Atomically redeem a code against a community's CURRENT server-computed premium bill.

    Security fix (task mandate): Node's `redeemDiscountCode()` accepts
    `originalPriceCents` as a raw request-body field and trusts it
    verbatim for the discount math -- a caller could redeem against a
    fabricated (e.g. inflated, to make a fixed-amount code net a bigger
    discount, or manipulated in other ways) base price. This port instead
    recomputes the original price itself from `community_premium_subscriptions`
    + live `marketplace_settings` pricing config (the same server-side
    calculation `calculate_monthly_bill()` uses for `/premium/subscribe`)
    -- the caller no longer supplies a price at all.
    """
    row = (
        dal(
            (dal.vendor_discount_codes.id == code_id)
            & (dal.vendor_discount_codes.is_active == True)  # noqa: E712
        )
        .select()
        .first()
    )
    now = dt.datetime.utcnow()
    if row is None or (row.valid_until and row.valid_until < now):
        raise conflict("Discount code is no longer valid or has reached its usage limit")
    if row.max_uses is not None and row.current_uses >= row.max_uses:
        raise conflict("Discount code is no longer valid or has reached its usage limit")

    base_price_cents, base_seat_limit, overage_price_cents = get_pricing_config(dal)
    seat_count = get_current_seat_count(dal, community_id)
    original_price_cents = calculate_monthly_bill(
        seat_count, base_price_cents, base_seat_limit, overage_price_cents
    )

    if row.discount_type == "free":
        discounted_price_cents = 0
    elif row.discount_type == "percentage":
        discount_amount = round(original_price_cents * (float(row.discount_value) / 100))
        discounted_price_cents = max(0, original_price_cents - discount_amount)
    else:  # fixed_amount -- discount_value is dollars (DECIMAL(10,2) per migration 064)
        discounted_price_cents = max(
            0, original_price_cents - round(float(row.discount_value) * 100)
        )

    discount_amount_cents = original_price_cents - discounted_price_cents

    dal(dal.vendor_discount_codes.id == code_id).update(
        current_uses=row.current_uses + 1, updated_at=now
    )
    redemption_id = dal.discount_code_redemptions.insert(
        discount_code_id=code_id,
        community_id=community_id,
        subscription_id=subscription_id,
        original_price_cents=original_price_cents,
        discounted_price_cents=discounted_price_cents,
        discount_amount_cents=discount_amount_cents,
        redeemed_at=now,
    )
    dal.commit()

    return DiscountRedemptionDTO(
        redemptionId=int(redemption_id),
        originalPriceCents=original_price_cents,
        discountedPriceCents=discounted_price_cents,
        discountAmountCents=discount_amount_cents,
    )
