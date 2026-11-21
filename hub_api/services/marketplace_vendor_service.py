"""Vendor self-service group -- profile, dashboard, modules, and role requests.

Ports `vendorController.js` + `vendorAnalyticsService.js` (marketplace_module)
and the vendor-role-request half of `vendorRequestController.js` (hub_module).

Every function here takes the raw sync `pydal` `dal` (not `AsyncDAL`) and is
called synchronously from inside async blueprint handlers, explicit
`dal.commit()` after every write -- matches this repo's established
non-M1 convention (`services/community_common.py`'s own docstring: "the
raw pydal dal ... is called synchronously from inside async handlers").
Complex reporting queries (analytics) go through `dal.executesql(sql,
placeholders=[...])` with STANDARD SQL only (JOIN/GROUP BY/HAVING/SUM/
COUNT) -- period-cutoff date arithmetic is computed in Python and passed
as a placeholder rather than using Postgres-only `DATE_TRUNC`/`INTERVAL`/
`FILTER (WHERE ...)`/`generate_series`, so every query here is portable
across `DB_TYPE` and exercised by this port's own sqlite test suite
(`hub_api/PORTING.md` Gotcha #1: "pydal query builder ... it's portable
and it's the only form these tests can exercise" -- extended here to
`executesql` calls too, not just the ORM builder).

**Vendor role requests**: `getVendorRequest`/`createVendorRequest` in
Node's `vendorController.js` (marketplace_module) is the LIVE, routed
implementation but is objectively weaker than hub_module's unrouted
`vendorRequestController.js`: it never checks for an existing pending/
approved request before inserting a duplicate, and its paired
`adminReviewController.approveVendorRequest` creates a `marketplace_sellers`
row instead of granting `hub_users.is_vendor` -- the flag the `vendor.js`
route file's own "Vendor self-service" comment implies gates the rest of
this module. `get_vendor_request`/`create_vendor_request` below port
hub's `vendorRequestController.js` logic (dedup check, `is_vendor` grant
on approval) instead, kept under the SAME `/api/v1/marketplace/vendor/
request` URL the frontend's pinned `vendorApi` contract expects
(`admin/hub_module/frontend/src/services/api.js`) -- same contract, the
objectively safer implementation behind it.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from services.errors import bad_request, conflict, forbidden, not_found
from services.url_guard import SSRFError, validate_url

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_URL_RE = re.compile(r"^https?://.+", re.IGNORECASE)
_VALID_PAYOUT_METHODS = frozenset({"stripe", "paypal", "bank_transfer"})
_ALLOWED_MODULE_UPDATE_FIELDS = frozenset(
    {
        "name",
        "description",
        "category",
        "webhookUrl",
        "webhookSecret",
        "webhookTimeoutMs",
        "triggerCommands",
        "triggerEvents",
        "requestedScopes",
        "pricingType",
        "priceCents",
        "pricingModel",
        "billingPeriod",
        "currency",
        "communicationModel",
        "authType",
        "authConfig",
        "apiBaseUrl",
        "integrationType",
    }
)
_MODULE_FIELD_TO_COLUMN = {
    "name": "name",
    "description": "description",
    "category": "category",
    "webhookUrl": "webhook_url",
    "webhookSecret": "webhook_secret",
    "webhookTimeoutMs": "webhook_timeout_ms",
    "triggerCommands": "trigger_commands",
    "triggerEvents": "trigger_events",
    "requestedScopes": "requested_scopes",
    "pricingType": "pricing_type",
    "priceCents": "price_cents",
    "pricingModel": "pricing_model",
    "billingPeriod": "billing_period",
    "currency": "currency",
    "communicationModel": "communication_model",
    "authType": "auth_type",
    "authConfig": "auth_config",
    "apiBaseUrl": "api_base_url",
    "integrationType": "integration_type",
}


def slugify(name: str) -> str:
    """Convert a display name to a URL-safe slug -- matches Node's `slugify()`."""
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def _row_to_seller_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "userId": row.user_id,
        "displayName": row.display_name,
        "description": row.description,
        "websiteUrl": row.website_url,
        "payoutMethod": row.payout_method,
        "totalRevenueCents": row.total_revenue_cents,
        "totalSubscribers": row.total_subscribers,
        "isVerified": bool(row.is_verified),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def get_vendor_profile(dal: Any, user_id: int) -> dict[str, Any] | None:
    """`GET /vendor/profile` -- the caller's own `marketplace_sellers` row, or `None`."""
    row = dal(dal.marketplace_sellers.user_id == user_id).select().first()
    return _row_to_seller_dict(row) if row else None


def _seller_id_for_user(dal: Any, user_id: int) -> int:
    row = dal(dal.marketplace_sellers.user_id == user_id).select(dal.marketplace_sellers.id).first()
    if row is None:
        raise not_found("Vendor profile not found")
    return int(row.id)


def create_vendor_profile(dal: Any, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """`POST /vendor/profile` -- create the caller's vendor profile. 409 if one exists."""
    if dal(dal.marketplace_sellers.user_id == user_id).select().first() is not None:
        raise conflict("Vendor profile already exists")
    now = datetime.now(UTC)
    row_id = dal.marketplace_sellers.insert(
        user_id=user_id,
        display_name=data.get("displayName"),
        description=data.get("description"),
        website_url=data.get("websiteUrl"),
        payout_method=data.get("payoutMethod"),
        created_at=now,
        updated_at=now,
    )
    dal.commit()
    row = dal.marketplace_sellers[row_id]
    return _row_to_seller_dict(row)


def update_vendor_profile(dal: Any, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """`PUT /vendor/profile` -- update the caller's own profile. 404 if none exists."""
    row = dal(dal.marketplace_sellers.user_id == user_id).select().first()
    if row is None:
        raise not_found("Vendor profile not found")
    updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if "displayName" in data and data["displayName"] is not None:
        updates["display_name"] = data["displayName"]
    if "description" in data and data["description"] is not None:
        updates["description"] = data["description"]
    if "websiteUrl" in data and data["websiteUrl"] is not None:
        updates["website_url"] = data["websiteUrl"]
    if "payoutMethod" in data and data["payoutMethod"] is not None:
        updates["payout_method"] = data["payoutMethod"]
    dal(dal.marketplace_sellers.id == row.id).update(**updates)
    dal.commit()
    return _row_to_seller_dict(dal.marketplace_sellers[row.id])


def validate_vendor_profile_input(data: dict[str, Any]) -> None:
    """Mirrors Node's `updateVendorProfile` inline validation -- raises `ApiError` on failure."""
    display_name = data.get("displayName")
    if display_name is not None and not str(display_name).strip():
        raise bad_request("displayName is required")
    website_url = data.get("websiteUrl")
    if website_url and not _URL_RE.match(website_url):
        raise bad_request("websiteUrl must be a valid URL starting with http:// or https://")
    payout_method = data.get("payoutMethod")
    if payout_method and payout_method not in _VALID_PAYOUT_METHODS:
        allowed = ", ".join(sorted(_VALID_PAYOUT_METHODS))
        raise bad_request(f"payoutMethod must be one of: {allowed}")


def get_vendor_modules(dal: Any, user_id: int, *, page: int = 1, limit: int = 25) -> dict[str, Any]:
    """`GET /vendor/modules` -- paginated modules owned by the caller."""
    seller = dal(dal.marketplace_sellers.user_id == user_id).select().first()
    if seller is None:
        return {"modules": [], "pagination": {"page": page, "limit": limit, "total": 0}}
    offset = (page - 1) * limit
    query = (dal.marketplace_modules.seller_id == seller.id) & (
        dal.marketplace_modules.deleted_at == None  # noqa: E711 -- pydal NULL comparison idiom
    )
    rows = dal(query).select(
        orderby=~dal.marketplace_modules.created_at, limitby=(offset, offset + limit)
    )
    modules = [_row_to_module_dict(r) for r in rows]
    return {"modules": modules, "pagination": {"page": page, "limit": limit, "total": len(modules)}}


def _row_to_module_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.public_id),
        "name": row.name,
        "slug": row.slug,
        "description": row.description,
        "category": row.category,
        "status": row.status,
        "webhookUrl": row.webhook_url,
        "communicationModel": row.communication_model,
        "integrationType": row.integration_type,
        "pricingType": row.pricing_type,
        "priceCents": row.price_cents,
        "installCount": row.install_count,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def get_vendor_dashboard(dal: Any, user_id: int) -> dict[str, Any]:
    """`GET /vendor/dashboard` -- stats, recent submissions, revenue breakdown."""
    seller_id = _seller_id_for_user(dal, user_id)

    modules = dal(dal.marketplace_modules.seller_id == seller_id).select()
    total_modules = len(modules)
    published_modules = sum(1 for m in modules if m.status == "approved")
    pending_review = sum(1 for m in modules if m.status == "pending")

    installs = (
        dal(dal.community_vendor_installations.module_id.belongs([m.id for m in modules])).select()
        if modules
        else []
    )
    total_installs = len(installs)

    payments = dal(dal.vendor_payments.seller_id == seller_id).select()
    total_revenue = sum(p.amount_cents or 0 for p in payments if p.status == "completed")
    expected_revenue = sum(p.amount_cents or 0 for p in payments if p.status == "pending")

    recent_submissions = (
        dal(dal.marketplace_submissions.module_id.belongs([m.id for m in modules])).select(
            orderby=~dal.marketplace_submissions.submitted_at, limitby=(0, 5)
        )
        if modules
        else []
    )
    module_names = {m.id: m.name for m in modules}
    module_uuids = {m.id: str(m.public_id) for m in modules}

    revenue_by_module: dict[int, int] = {}
    for p in payments:
        if p.module_id is not None:
            revenue_by_module[p.module_id] = revenue_by_module.get(p.module_id, 0) + (
                p.amount_cents or 0
            )

    return {
        "stats": {
            "totalModules": total_modules,
            "publishedModules": published_modules,
            "pendingReview": pending_review,
            "totalInstalls": total_installs,
            "totalRevenue": total_revenue,
            "expectedRevenue": expected_revenue,
        },
        "recentSubmissions": [
            {
                "id": str(s.public_id),
                "moduleId": module_uuids.get(s.module_id),
                "status": s.status,
                "submittedAt": s.submitted_at.isoformat() if s.submitted_at else None,
                "moduleName": module_names.get(s.module_id),
            }
            for s in recent_submissions
        ],
        "revenueBreakdown": [
            {"moduleId": module_uuids.get(mid), "moduleName": module_names.get(mid), "revenue": rev}
            for mid, rev in sorted(revenue_by_module.items(), key=lambda kv: -kv[1])
        ],
    }


def get_vendor_analytics_overview(dal: Any, user_id: int) -> dict[str, Any]:
    """`GET /vendor/analytics/overview` -- basic summary across the caller's modules."""
    seller_id = _seller_id_for_user(dal, user_id)
    module_rows = dal(dal.marketplace_modules.seller_id == seller_id).select(
        dal.marketplace_modules.id
    )
    module_ids = [m.id for m in module_rows]
    installs = (
        dal(dal.community_vendor_installations.module_id.belongs(module_ids)).select()
        if module_ids
        else []
    )
    total_installs = len(installs)
    total_uninstalls = sum(1 for i in installs if i.status == "uninstalled")

    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    installs_this_month = sum(
        1 for i in installs if i.installed_at and _as_aware(i.installed_at) >= month_start
    )

    payments = dal(dal.vendor_payments.seller_id == seller_id).select() if module_ids else []
    revenue_this_month = sum(
        p.amount_cents or 0 for p in payments if p.paid_at and _as_aware(p.paid_at) >= month_start
    )

    reviews = (
        dal(dal.vendor_module_reviews.vendor_module_id.belongs(module_ids)).select()
        if module_ids
        else []
    )
    avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 2) if reviews else None

    return {
        "totalInstalls": total_installs,
        "totalUninstalls": total_uninstalls,
        "installsThisMonth": installs_this_month,
        "revenueThisMonth": revenue_this_month,
        "avgRating": avg_rating,
    }


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def create_vendor_module(dal: Any, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """`POST /vendor/modules` -- create a module. Requires an existing vendor profile.

    SSRF guard: `webhookUrl`/`apiBaseUrl` are vendor-controlled and used
    server-side for every future command execution -- rejected here at
    write time (`url_guard.validate_url`), same as
    `bot_ai_knowledge.py`'s write-time check for user-supplied source URLs.
    """
    seller = dal(dal.marketplace_sellers.user_id == user_id).select().first()
    if seller is None:
        raise forbidden("Vendor profile required. Please create a vendor profile first.")

    webhook_url = data.get("webhookUrl")
    if not webhook_url:
        raise bad_request("webhookUrl is required")
    _guard_module_urls(webhook_url, data.get("apiBaseUrl"))

    name = data.get("name")
    if not name:
        raise bad_request("name is required")
    slug = slugify(name)
    now = datetime.now(UTC)
    insert_kwargs = _module_insert_kwargs(seller.id, name, slug, data, now)
    try:
        module_id = dal.marketplace_modules.insert(**insert_kwargs)
        dal.commit()
    except Exception:  # noqa: BLE001 -- unique(slug) collision, mirrors Node's err.code === '23505'
        dal.rollback()
        insert_kwargs["slug"] = f"{slug}-{user_id}"
        module_id = dal.marketplace_modules.insert(**insert_kwargs)
        dal.commit()

    row = dal.marketplace_modules[module_id]
    return {
        "id": str(row.public_id),
        "slug": row.slug,
        "createdAt": row.created_at.isoformat(),
    }


def _guard_module_urls(webhook_url: str, api_base_url: str | None) -> None:
    try:
        validate_url(webhook_url)
        if api_base_url:
            validate_url(api_base_url)
    except SSRFError as exc:
        raise bad_request(f"Rejected module URL: {exc}") from exc


def _module_insert_kwargs(
    seller_id: int, name: str, slug: str, data: dict[str, Any], now: datetime
) -> dict[str, Any]:
    return {
        "seller_id": seller_id,
        "name": name,
        "slug": slug,
        "description": data.get("description"),
        "category": data.get("category"),
        "webhook_url": data.get("webhookUrl"),
        "webhook_secret": data.get("webhookSecret"),
        "webhook_timeout_ms": data.get("webhookTimeoutMs", 5000),
        "trigger_commands": data.get("triggerCommands", []),
        "trigger_events": data.get("triggerEvents", []),
        "requested_scopes": data.get("requestedScopes", []),
        "response_types": data.get("responseTypes", []),
        "pricing_type": data.get("pricingType", "free"),
        "price_cents": data.get("priceCents", 0),
        "pricing_model": data.get("pricingModel", "flat"),
        "billing_period": data.get("billingPeriod", "monthly"),
        "currency": data.get("currency", "USD"),
        "communication_model": data.get("communicationModel", "webhook_push"),
        "auth_type": data.get("authType", "hmac"),
        "auth_config": data.get("authConfig", {}),
        "api_base_url": data.get("apiBaseUrl"),
        "integration_type": data.get("integrationType", "command_handler"),
        "created_at": now,
        "updated_at": now,
    }


def update_vendor_module(
    dal: Any, user_id: int, module_id: int, updates: dict[str, Any]
) -> dict[str, Any]:
    """`PUT /vendor/modules/<id>` -- IDOR-safe: only the owning vendor may update.

    Ownership is re-checked by joining `marketplace_sellers.user_id ==
    caller` against the target module's `seller_id`, exactly like Node's
    `ownerCheck` query -- a mismatch (module belongs to a different
    vendor) 404s rather than 403ing, matching Node's own behavior (no
    existence leak beyond "not found").
    """
    module = _owned_module_or_404(dal, user_id, module_id)
    set_fields: dict[str, Any] = {}
    for key in _ALLOWED_MODULE_UPDATE_FIELDS:
        if key in updates:
            value = updates[key]
            if key in ("webhookUrl", "apiBaseUrl") and value:
                try:
                    validate_url(value)
                except SSRFError as exc:
                    raise bad_request(f"Rejected module URL: {exc}") from exc
            set_fields[_MODULE_FIELD_TO_COLUMN[key]] = value
    if not set_fields:
        return {"success": True}
    set_fields["updated_at"] = datetime.now(UTC)
    dal(dal.marketplace_modules.id == module.id).update(**set_fields)
    dal.commit()
    return {"success": True}


def _owned_module_or_404(dal: Any, user_id: int, module_id: int) -> Any:
    seller = dal(dal.marketplace_sellers.user_id == user_id).select().first()
    if seller is None:
        raise not_found("Module not found")
    module = (
        dal(
            (dal.marketplace_modules.id == module_id)
            & (dal.marketplace_modules.seller_id == seller.id)
            & (dal.marketplace_modules.deleted_at == None)  # noqa: E711
        )
        .select()
        .first()
    )
    if module is None:
        raise not_found("Module not found")
    return module


def submit_module_for_review(
    dal: Any, user_id: int, module_id: int, changes_description: str | None
) -> dict[str, Any]:
    """`POST /vendor/modules/<id>/submit` -- IDOR-safe via `_owned_module_or_404`."""
    module = _owned_module_or_404(dal, user_id, module_id)
    now = datetime.now(UTC)
    submission_id = dal.marketplace_submissions.insert(
        module_id=module.id,
        version=module.version,
        changes_description=changes_description,
        submitted_by=user_id,
        status="pending",
        submitted_at=now,
    )
    dal(dal.marketplace_modules.id == module.id).update(status="pending", updated_at=now)
    dal.commit()
    return {"submissionId": str(dal.marketplace_submissions[submission_id].public_id)}


# ---------------------------------------------------------------------------
# Vendor role requests -- ports hub_module's vendorRequestController.js (see
# module docstring for why this, not marketplace's weaker version).
# ---------------------------------------------------------------------------


def get_vendor_request(dal: Any, user_id: int) -> dict[str, Any] | None:
    """`GET /vendor/request` -- the caller's most recent vendor-role request."""
    row = (
        dal(dal.vendor_role_requests.user_id == user_id)
        .select(orderby=~dal.vendor_role_requests.requested_at, limitby=(0, 1))
        .first()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "requestId": row.request_id,
        "status": row.status,
        "companyName": row.company_name,
        "rejectionReason": row.rejection_reason,
        "requestedAt": row.requested_at.isoformat() if row.requested_at else None,
        "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


def create_vendor_request(
    dal: Any, user_id: int, user_email: str, user_display_name: str | None, data: dict[str, Any]
) -> dict[str, Any]:
    """`POST /vendor/request` -- dedup-checked (pending/approved) before inserting."""
    company_name = data.get("companyName")
    business_description = data.get("businessDescription")
    contact_email = data.get("contactEmail")
    if not company_name or not business_description or not contact_email:
        raise bad_request("Company Name, Business Description, and Contact Email are required")

    existing = (
        dal(
            (dal.vendor_role_requests.user_id == user_id)
            & (dal.vendor_role_requests.status.belongs(["pending", "approved"]))
        )
        .select(orderby=~dal.vendor_role_requests.requested_at, limitby=(0, 1))
        .first()
    )
    if existing is not None:
        if existing.status == "approved":
            raise bad_request("User already has an approved vendor request")
        raise bad_request("User already has a pending vendor request")

    now = datetime.now(UTC)
    request_id = str(uuid.uuid4())
    row_id = dal.vendor_role_requests.insert(
        request_id=request_id,
        user_id=user_id,
        user_email=user_email,
        user_display_name=user_display_name,
        company_name=company_name,
        company_website=data.get("companyWebsite"),
        business_description=business_description,
        experience_summary=data.get("experienceSummary", ""),
        contact_email=contact_email,
        contact_phone=data.get("contactPhone", ""),
        status="pending",
        requested_at=now,
        created_at=now,
        updated_at=now,
    )
    dal.commit()
    row = dal.vendor_role_requests[row_id]
    return {
        "id": row.id,
        "requestId": row.request_id,
        "status": row.status,
        "requestedAt": row.requested_at.isoformat() if row.requested_at else None,
        "companyName": row.company_name,
    }
