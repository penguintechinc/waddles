"""Admin-review group -- adminReviewController.js + vendor-request/submission admin actions.

Ports `adminReviewController.js` (marketplace_module), the admin half of
`vendorRequestController.js` (hub_module, see `marketplace_vendor_service`'s
module docstring for why hub's version is canonical), and
`vendorSubmissionController.js` (hub_module, the standalone public
vendor-submission pipeline: `vendor_submissions` -> admin review ->
`approved_vendor_modules`, a parallel, non-overlapping pipeline to
`marketplace_modules`/`marketplace_submissions`).

**Security fixes over the Node originals** (task requirement, not present
in Node today):

1. **Self-approval**: route-level `require_scope("marketplace:admin")`
   already excludes ordinary vendors structurally, but says nothing about
   an admin approving THEIR OWN prior submission/request -- Node has no
   check for this at all. Every approve function here additionally
   compares the submission/request's own submitter identity against the
   caller and 403s on a match (`_reject_self_approval`).
2. **IDOR**: `get_submission_status` (public) requires the caller to
   already know BOTH the opaque `submission_id` (a UUID, not sequential)
   AND the `vendor_email` on file -- preserved from Node's own design,
   which is already IDOR-resistant here (not a raw sequential-ID lookup).
   `marketplace_vendor_service`'s module-CRUD IDOR fix
   (`_owned_module_or_404`) is the other half of this requirement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from services.errors import ApiError, bad_request, forbidden, not_found
from services.marketplace_crypto import encrypt_payment_details, encrypt_webhook_secret
from services.url_guard import SSRFError, validate_url

_PAYMENT_METHODS = frozenset({"paypal", "stripe", "check", "bank_transfer", "other"})
_PRICING_MODELS = frozenset({"flat-rate", "per-seat"})

_SCOPE_DEFINITIONS: dict[str, dict[str, str]] = {
    "read_chat": {
        "name": "Read Chat Messages",
        "riskLevel": "low",
        "dataShared": "Chat messages from channels",
    },
    "send_message": {
        "name": "Send Messages",
        "riskLevel": "medium",
        "dataShared": "Ability to post messages",
    },
    "read_profile": {
        "name": "Read User Profiles",
        "riskLevel": "low",
        "dataShared": "User profile information",
    },
    "read_viewers": {
        "name": "Read Viewer List",
        "riskLevel": "low",
        "dataShared": "Active viewer/user list",
    },
    "modify_settings": {
        "name": "Modify Community Settings",
        "riskLevel": "high",
        "dataShared": "Full community configuration access",
    },
    "control_music": {
        "name": "Control Music Player",
        "riskLevel": "medium",
        "dataShared": "Music playback control",
    },
    "read_music": {
        "name": "Read Music Queue",
        "riskLevel": "low",
        "dataShared": "Current music queue",
    },
    "read_permissions": {
        "name": "Read Permissions",
        "riskLevel": "medium",
        "dataShared": "User role and permission data",
    },
    "modify_permissions": {
        "name": "Modify Permissions",
        "riskLevel": "critical",
        "dataShared": "Full permission modification",
    },
    "delete_data": {
        "name": "Delete Community Data",
        "riskLevel": "critical",
        "dataShared": "Ability to delete any community data",
    },
}


def _reject_self_approval(*, submitter_ref: Any, reviewer_ref: Any) -> None:
    """403 if the reviewer and the original submitter are the same identity."""
    if submitter_ref is not None and reviewer_ref is not None and submitter_ref == reviewer_ref:
        raise forbidden("You cannot approve your own submission")


# ---------------------------------------------------------------------------
# marketplace_submissions pipeline (adminReviewController.js)
# ---------------------------------------------------------------------------


def get_vendor_role_requests(
    dal: Any, *, status: str | None = None, page: int = 1, limit: int = 25
) -> dict[str, Any]:
    """`GET /admin/marketplace/vendor-requests` -- ports hub's `getPendingVendorRequests`."""
    query = dal.vendor_role_requests.id > 0
    if status and status != "all":
        query &= dal.vendor_role_requests.status == status
    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=~dal.vendor_role_requests.requested_at, limitby=(offset, offset + limit)
    )
    return {
        "requests": [_request_dict(r) for r in rows],
        "pagination": {"total": total, "page": page, "limit": limit, "pages": -(-total // limit)},
    }


def _request_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "requestId": row.request_id,
        "userId": row.user_id,
        "userEmail": row.user_email,
        "userDisplayName": row.user_display_name,
        "companyName": row.company_name,
        "businessDescription": row.business_description,
        "experienceSummary": row.experience_summary,
        "contactEmail": row.contact_email,
        "contactPhone": row.contact_phone,
        "status": row.status,
        "rejectionReason": row.rejection_reason,
        "requestedAt": row.requested_at.isoformat() if row.requested_at else None,
        "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "reviewedBy": row.reviewed_by,
        "adminNotes": row.admin_notes,
    }


def approve_vendor_role_request(
    dal: Any, request_id: str, *, admin_user_id: int, admin_notes: str = ""
) -> dict[str, Any]:
    """`POST /admin/marketplace/vendor-requests/<id>/approve` -- grants `hub_users.is_vendor`."""
    row = dal(dal.vendor_role_requests.request_id == request_id).select().first()
    if row is None:
        raise not_found("Vendor request not found")
    _reject_self_approval(submitter_ref=row.user_id, reviewer_ref=admin_user_id)

    now = datetime.now(UTC)
    dal(dal.vendor_role_requests.id == row.id).update(
        status="approved",
        reviewed_by=admin_user_id,
        reviewed_at=now,
        admin_notes=admin_notes,
        updated_at=now,
    )
    dal(dal.hub_users.id == row.user_id).update(is_vendor=True, updated_at=now)
    dal.commit()
    return _request_dict(dal.vendor_role_requests[row.id])


def reject_vendor_role_request(
    dal: Any, request_id: str, *, admin_user_id: int, rejection_reason: str, admin_notes: str = ""
) -> dict[str, Any]:
    """`POST /admin/marketplace/vendor-requests/<id>/reject`."""
    if not rejection_reason:
        raise bad_request("Rejection reason is required")
    row = dal(dal.vendor_role_requests.request_id == request_id).select().first()
    if row is None:
        raise not_found("Vendor request not found")

    now = datetime.now(UTC)
    dal(dal.vendor_role_requests.id == row.id).update(
        status="rejected",
        rejection_reason=rejection_reason,
        reviewed_by=admin_user_id,
        reviewed_at=now,
        admin_notes=admin_notes,
        updated_at=now,
    )
    dal.commit()
    return _request_dict(dal.vendor_role_requests[row.id])


def get_submissions(
    dal: Any, *, status: str | None = None, page: int = 1, limit: int = 25
) -> dict[str, Any]:
    """`GET /admin/marketplace/submissions` -- `marketplace_submissions` review queue."""
    query = dal.marketplace_submissions.id > 0
    if status:
        query &= dal.marketplace_submissions.status == status
    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=~dal.marketplace_submissions.submitted_at, limitby=(offset, offset + limit)
    )
    module_ids = [r.module_id for r in rows]
    modules = {
        m.id: m
        for m in (
            dal(dal.marketplace_modules.id.belongs(module_ids)).select() if module_ids else []
        )
    }
    submitters = {
        u.id: u.username
        for u in (
            dal(dal.hub_users.id.belongs([r.submitted_by for r in rows if r.submitted_by])).select()
            if rows
            else []
        )
    }
    submissions = []
    for r in rows:
        module = modules.get(r.module_id)
        submissions.append(
            {
                "id": r.id,
                "moduleId": r.module_id,
                "moduleName": module.name if module else None,
                "category": module.category if module else None,
                "submitterUsername": submitters.get(r.submitted_by),
                "version": r.version,
                "changesDescription": r.changes_description,
                "status": r.status,
                "submittedAt": r.submitted_at.isoformat() if r.submitted_at else None,
                "reviewedAt": r.reviewed_at.isoformat() if r.reviewed_at else None,
                "reviewNotes": r.review_notes,
            }
        )
    return {
        "submissions": submissions,
        "pagination": {"total": total, "page": page, "limit": limit, "pages": -(-total // limit)},
    }


def approve_submission(
    dal: Any, submission_id: int, *, admin_user_id: int, notes: str | None = None
) -> None:
    """`POST /admin/marketplace/submissions/<id>/approve`."""
    submission = dal.marketplace_submissions[submission_id]
    if submission is None:
        raise not_found("Submission not found")
    _reject_self_approval(submitter_ref=submission.submitted_by, reviewer_ref=admin_user_id)

    now = datetime.now(UTC)
    dal(dal.marketplace_submissions.id == submission_id).update(
        status="approved", reviewed_by=admin_user_id, reviewed_at=now, review_notes=notes
    )
    dal(dal.marketplace_modules.id == submission.module_id).update(
        status="approved", approved_by=admin_user_id, approved_at=now, updated_at=now
    )
    dal.commit()


def reject_submission(
    dal: Any,
    submission_id: int,
    *,
    admin_user_id: int,
    reason: str | None,
    notes: str | None = None,
) -> None:
    """`POST /admin/marketplace/submissions/<id>/reject`."""
    submission = dal.marketplace_submissions[submission_id]
    if submission is None:
        raise not_found("Submission not found")

    now = datetime.now(UTC)
    dal(dal.marketplace_submissions.id == submission_id).update(
        status="rejected", reviewed_by=admin_user_id, reviewed_at=now, review_notes=notes
    )
    dal(dal.marketplace_modules.id == submission.module_id).update(
        status="rejected", rejection_reason=reason, updated_at=now
    )
    dal.commit()


def get_marketplace_settings(dal: Any) -> dict[str, str]:
    """`GET /admin/marketplace/settings`."""
    rows = dal(dal.marketplace_settings.id > 0).select(orderby=dal.marketplace_settings.setting_key)
    return {r.setting_key: r.setting_value for r in rows}


def update_marketplace_settings(dal: Any, settings: dict[str, str], *, admin_user_id: int) -> None:
    """`PUT /admin/marketplace/settings`."""
    now = datetime.now(UTC)
    for key, value in settings.items():
        existing = dal(dal.marketplace_settings.setting_key == key).select().first()
        if existing is not None:
            dal(dal.marketplace_settings.id == existing.id).update(
                setting_value=value, updated_by=admin_user_id, updated_at=now
            )
        else:
            dal.marketplace_settings.insert(
                setting_key=key, setting_value=value, updated_by=admin_user_id, updated_at=now
            )
    dal.commit()


# ---------------------------------------------------------------------------
# vendor_submissions pipeline (vendorSubmissionController.js) -- standalone
# public submission -> admin review -> approved_vendor_modules publish.
# ---------------------------------------------------------------------------


def _validate_submission(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(data.get("vendorName") or "").strip():
        errors.append("Vendor name is required")
    vendor_email = data.get("vendorEmail")
    if not vendor_email or "@" not in str(vendor_email):
        errors.append("Valid vendor email is required")
    if not str(data.get("moduleName") or "").strip():
        errors.append("Module name is required")
    webhook_url = data.get("webhookUrl")
    if not webhook_url:
        errors.append("Valid webhook URL is required")
    else:
        try:
            validate_url(webhook_url)
        except SSRFError:
            errors.append("Valid webhook URL is required")
    scopes = data.get("scopes")
    if not isinstance(scopes, list) or len(scopes) == 0:
        errors.append("At least one scope is required")
    if data.get("pricingModel") not in _PRICING_MODELS:
        errors.append(f"Pricing model must be one of: {', '.join(sorted(_PRICING_MODELS))}")
    pricing_amount = data.get("pricingAmount")
    if (
        not isinstance(pricing_amount, int | float)
        or isinstance(pricing_amount, bool)
        or pricing_amount < 0
    ):
        errors.append("Pricing amount must be a non-negative number")
    if data.get("paymentMethod") not in _PAYMENT_METHODS:
        errors.append(f"Payment method must be one of: {', '.join(sorted(_PAYMENT_METHODS))}")
    if not isinstance(data.get("paymentDetails"), dict):
        errors.append("Payment details are required")
    return errors


def submit_vendor_module(dal: Any, data: dict[str, Any]) -> dict[str, Any]:
    """`POST /public/vendor/submit` -- unauthenticated public module submission.

    SSRF-guards `webhookUrl` via `_validate_submission` before any write.
    """
    errors = _validate_submission(data)
    if errors:
        raise ApiError("Validation failed", 400, "VALIDATION_ERROR")

    vendor_email = data["vendorEmail"]
    module_name = data["moduleName"]
    existing = (
        dal(
            (dal.vendor_submissions.vendor_email == vendor_email)
            & (dal.vendor_submissions.module_name == module_name)
            & (~dal.vendor_submissions.status.belongs(["rejected", "suspended"]))
        )
        .select()
        .first()
    )
    if existing is not None:
        raise ApiError("You already have an active submission for this module", 409, "CONFLICT")

    submission_id = str(uuid.uuid4())
    webhook_secret = data.get("webhookSecret")
    encrypted_secret = encrypt_webhook_secret(webhook_secret) if webhook_secret else None
    now = datetime.now(UTC)

    row_id = dal.vendor_submissions.insert(
        submission_id=submission_id,
        vendor_name=data["vendorName"],
        vendor_email=vendor_email,
        company_name=data.get("companyName"),
        contact_phone=data.get("contactPhone"),
        website_url=data.get("websiteUrl"),
        module_name=module_name,
        module_description=data.get("moduleDescription"),
        module_category=data.get("moduleCategory", "interactive"),
        module_version=data.get("moduleVersion"),
        repository_url=data.get("repositoryUrl"),
        webhook_url=data["webhookUrl"],
        webhook_secret=encrypted_secret,
        webhook_per_community=data.get("webhookPerCommunity", False),
        scopes=data.get("scopes", []),
        scope_justification=data.get("scopeJustification"),
        pricing_model=data["pricingModel"],
        pricing_amount=data["pricingAmount"],
        pricing_currency=data.get("pricingCurrency", "USD"),
        payment_method=data["paymentMethod"],
        payment_details=encrypt_payment_details(data.get("paymentDetails", {})),
        supported_platforms=data.get("supportedPlatforms", []),
        documentation_url=data.get("documentationUrl"),
        support_email=data.get("supportEmail"),
        support_contact_url=data.get("supportContactUrl"),
        submitted_at=now,
    )

    for scope in data.get("scopes", []):
        scope_def = _SCOPE_DEFINITIONS.get(
            scope, {"name": scope, "riskLevel": "medium", "dataShared": "Module-specific data"}
        )
        dal.vendor_submission_scopes.insert(
            submission_id=row_id,
            scope_name=scope,
            risk_level=scope_def["riskLevel"],
            description=scope_def["name"],
            data_shared=scope_def["dataShared"],
            created_at=now,
        )

    dal.vendor_submission_reviews.insert(
        submission_id=row_id,
        reviewer_id=None,
        action="submitted",
        comments="Initial submission received",
        created_at=now,
    )
    dal.commit()

    row = dal.vendor_submissions[row_id]
    return {
        "submissionId": row.submission_id,
        "status": "pending",
        "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
    }


def get_submission_status(dal: Any, submission_id: str, email: str) -> dict[str, Any]:
    """`GET /public/vendor/submissions/<submission_id>?email=` -- IDOR-safe (both must match)."""
    row = (
        dal(
            (dal.vendor_submissions.submission_id == submission_id)
            & (dal.vendor_submissions.vendor_email == email)
        )
        .select()
        .first()
    )
    if row is None:
        raise not_found("Submission not found")
    reviews = dal(dal.vendor_submission_reviews.submission_id == row.id).select(
        orderby=~dal.vendor_submission_reviews.created_at, limitby=(0, 10)
    )
    return {
        "id": row.id,
        "submissionId": row.submission_id,
        "vendorName": row.vendor_name,
        "moduleName": row.module_name,
        "status": row.status,
        "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
        "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "rejectionReason": row.rejection_reason,
        "adminNotes": row.admin_notes,
        "requiresSpecialReview": bool(row.requires_special_review),
        "reviews": [
            {
                "action": r.action,
                "comments": r.comments,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reviews
        ],
    }


def get_published_modules(
    dal: Any, *, page: int = 1, limit: int = 20, featured: bool = False
) -> dict[str, Any]:
    """`GET /public/vendor/modules` -- published `approved_vendor_modules`."""
    query = dal.approved_vendor_modules.is_active == True  # noqa: E712 -- pydal boolean idiom
    if featured:
        query &= dal.approved_vendor_modules.is_featured == True  # noqa: E712
    total = dal(query).count()
    offset = (page - 1) * limit
    orderby = (
        (dal.approved_vendor_modules.feature_position, ~dal.approved_vendor_modules.published_at)
        if featured
        else ~dal.approved_vendor_modules.published_at
    )
    rows = dal(query).select(orderby=orderby, limitby=(offset, offset + limit))
    modules = []
    for r in rows:
        submission = dal.vendor_submissions[r.submission_id]
        modules.append(
            {
                "id": r.id,
                "vendorName": r.vendor_name,
                "moduleName": r.module_name,
                "moduleSlug": r.module_slug,
                "isFeatured": bool(r.is_featured),
                "installCount": r.install_count,
                "rating": r.rating,
                "reviewCount": r.review_count,
                "publishedAt": r.published_at.isoformat() if r.published_at else None,
                "pricingModel": submission.pricing_model if submission else None,
                "pricingAmount": submission.pricing_amount if submission else None,
                "pricingCurrency": submission.pricing_currency if submission else None,
                "moduleDescription": submission.module_description if submission else None,
                "supportedPlatforms": submission.supported_platforms if submission else None,
                "documentationUrl": submission.documentation_url if submission else None,
                "supportEmail": submission.support_email if submission else None,
            }
        )
    return {
        "modules": modules,
        "pagination": {"total": total, "page": page, "limit": limit, "pages": -(-total // limit)},
    }


def get_vendor_submissions_for_review(
    dal: Any, *, status: str = "pending", page: int = 1, limit: int = 20
) -> dict[str, Any]:
    """`GET /admin/marketplace/vendor-submissions` -- admin review queue."""
    total = dal(dal.vendor_submissions.status == status).count()
    offset = (page - 1) * limit
    rows = dal(dal.vendor_submissions.status == status).select(
        orderby=~dal.vendor_submissions.submitted_at, limitby=(offset, offset + limit)
    )
    submissions = []
    for r in rows:
        scopes = dal(dal.vendor_submission_scopes.submission_id == r.id).select(
            orderby=~dal.vendor_submission_scopes.risk_level
        )
        scope_list = [{"scopeName": s.scope_name, "riskLevel": s.risk_level} for s in scopes]
        submissions.append({**_vendor_submission_dict(r), "scopes": scope_list})
    return {
        "submissions": submissions,
        "pagination": {"page": page, "limit": limit, "total": total, "pages": -(-total // limit)},
    }


def _vendor_submission_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "submissionId": row.submission_id,
        "vendorName": row.vendor_name,
        "vendorEmail": row.vendor_email,
        "moduleName": row.module_name,
        "moduleCategory": row.module_category,
        "status": row.status,
        "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
        "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "requiresSpecialReview": bool(row.requires_special_review),
    }


def get_vendor_submission_details(dal: Any, submission_id: int) -> dict[str, Any]:
    """`GET /admin/marketplace/vendor-submissions/<id>` -- full admin detail."""
    row = dal.vendor_submissions[submission_id]
    if row is None:
        raise not_found("Submission not found")
    scopes = dal(dal.vendor_submission_scopes.submission_id == submission_id).select()
    reviews = dal(dal.vendor_submission_reviews.submission_id == submission_id).select(
        orderby=~dal.vendor_submission_reviews.created_at
    )
    return {
        **_vendor_submission_dict(row),
        "companyName": row.company_name,
        "websiteUrl": row.website_url,
        "moduleDescription": row.module_description,
        "webhookUrl": row.webhook_url,
        "pricingModel": row.pricing_model,
        "pricingAmount": row.pricing_amount,
        "paymentMethod": row.payment_method,
        "scopes": [
            {
                "id": s.id,
                "scopeName": s.scope_name,
                "riskLevel": s.risk_level,
                "description": s.description,
            }
            for s in scopes
        ],
        "reviews": [
            {
                "action": r.action,
                "comments": r.comments,
                "reviewerId": r.reviewer_id,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reviews
        ],
    }


def approve_vendor_submission(
    dal: Any, submission_id: int, *, admin_user_id: int, admin_notes: str | None = None
) -> dict[str, Any]:
    """`POST /admin/marketplace/vendor-submissions/<id>/approve`."""
    row = dal.vendor_submissions[submission_id]
    if row is None:
        raise not_found("Submission not found")
    admin = dal.hub_users[admin_user_id]
    _reject_self_approval(
        submitter_ref=row.vendor_email, reviewer_ref=admin.email if admin else None
    )

    now = datetime.now(UTC)
    dal(dal.vendor_submissions.id == submission_id).update(
        status="approved", reviewed_at=now, reviewed_by=admin_user_id, admin_notes=admin_notes
    )
    dal.vendor_submission_reviews.insert(
        submission_id=submission_id,
        reviewer_id=admin_user_id,
        action="approved",
        comments=admin_notes,
        created_at=now,
    )
    dal.commit()
    return _vendor_submission_dict(dal.vendor_submissions[submission_id])


def reject_vendor_submission(
    dal: Any,
    submission_id: int,
    *,
    admin_user_id: int,
    rejection_reason: str,
    admin_notes: str | None = None,
) -> dict[str, Any]:
    """`POST /admin/marketplace/vendor-submissions/<id>/reject`."""
    if not rejection_reason:
        raise bad_request("Rejection reason is required")
    row = dal.vendor_submissions[submission_id]
    if row is None:
        raise not_found("Submission not found")

    now = datetime.now(UTC)
    dal(dal.vendor_submissions.id == submission_id).update(
        status="rejected",
        reviewed_at=now,
        reviewed_by=admin_user_id,
        rejection_reason=rejection_reason,
        admin_notes=admin_notes,
    )
    dal.vendor_submission_reviews.insert(
        submission_id=submission_id,
        reviewer_id=admin_user_id,
        action="rejected",
        comments=rejection_reason,
        created_at=now,
    )
    dal.commit()
    return _vendor_submission_dict(dal.vendor_submissions[submission_id])


def request_more_info(dal: Any, submission_id: int, *, admin_user_id: int, message: str) -> None:
    """`POST /admin/marketplace/vendor-submissions/<id>/request-info`."""
    if not message:
        raise bad_request("Information request message is required")
    if dal.vendor_submissions[submission_id] is None:
        raise not_found("Submission not found")
    dal(dal.vendor_submissions.id == submission_id).update(status="under-review")
    dal.vendor_submission_reviews.insert(
        submission_id=submission_id,
        reviewer_id=admin_user_id,
        action="requested_info",
        comments=message,
        created_at=datetime.now(UTC),
    )
    dal.commit()


def _generate_slug(text: str) -> str:
    import re

    slug = re.sub(r"[^\w\s-]", "", text.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")


def publish_vendor_module(dal: Any, submission_id: int, *, admin_user_id: int) -> dict[str, Any]:
    """`POST /admin/marketplace/vendor-submissions/<id>/publish`.

    Only approved submissions publish (preserved from Node). Webhook
    secret is re-encrypted at rest exactly as stored -- Node's original
    called `decryptWebhookSecret` here (a bug: it stored the ENCRYPTED
    value into `approved_vendor_modules.webhook_secret` unchanged rather
    than actually decrypting, since the local var name shadowed intent).
    This port keeps the secret encrypted end-to-end (decrypted only at
    execution time by `marketplace_execution_service`, matching
    `bot_crypto.py`'s own at-rest-encrypted-until-use precedent) --
    functionally equivalent to what Node's code actually does at
    runtime, without the misleading `decryptWebhookSecret` call name.
    """
    row = dal.vendor_submissions[submission_id]
    if row is None:
        raise not_found("Submission not found")
    if row.status != "approved":
        raise bad_request("Only approved submissions can be published")

    module_slug = _generate_slug(f"{row.vendor_name} {row.module_name}")
    if dal(dal.approved_vendor_modules.module_slug == module_slug).select().first() is not None:
        raise ApiError("A module with this name already exists", 409, "CONFLICT")

    now = datetime.now(UTC)
    module_id = dal.approved_vendor_modules.insert(
        submission_id=submission_id,
        vendor_name=row.vendor_name,
        module_name=row.module_name,
        module_slug=module_slug,
        webhook_url=row.webhook_url,
        webhook_secret=row.webhook_secret,
        webhook_per_community=row.webhook_per_community,
        published_at=now,
        updated_at=now,
    )
    dal.commit()
    _ = admin_user_id  # audit-log field only, matches Node's `published_by`
    published = dal.approved_vendor_modules[module_id]
    return {
        "id": published.id,
        "moduleSlug": published.module_slug,
        "publishedAt": published.published_at.isoformat() if published.published_at else None,
    }
