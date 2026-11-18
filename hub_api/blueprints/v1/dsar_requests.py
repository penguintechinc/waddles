"""v1 DSAR request inbox -- admin inbox, logged-in intake, public captcha + email-verified form.

Level 1 of the two-level DSAR system: statutory, every tier, NEVER tier-gated. See
`services/dsar_service.py` for enums, the SLA clock, the verification trust model and retention.
The flag `waddles.dsar-inbox` controls only the ROLLOUT of this new inbox (404
`DSAR_INBOX_DISABLED` when off); the existing manual export/erase/do-not-sell routes in
`admin_data_privacy.py` and the self-service `/user/me/data` routes never consult it.

Three blueprints:

| Blueprint | Prefix                          | Auth                                                     |
|-----------|---------------------------------|----------------------------------------------------------|
| admin     | `/api/v1/tenant/<slug>/privacy` | `tenant_middleware` + `tenant:admin` + matching tenant   |
| user      | `/api/v1/user/me/privacy-requests` | `tenant_middleware` (bearer JWT), tenant from the token |
| public    | `/api/v1/public`                | none -- captcha + email verification + rate limits       |

Public-route rules: the tenant comes ONLY from the URL slug; a submission never reveals whether a
tenant slug or email exists (every well-formed, captcha-passing submission gets the same 202);
body size is capped; captcha is verified server-side and FAILS CLOSED (503) when unconfigured or
the provider is unreachable. Responses use explicit DTOs via `jsonify_dto` (security.md Output
Validation) and only ever carry UUIDs across the boundary.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import document_response, validate_request

from blueprints.v1.admin_data_privacy import (
    _HTTP_STATUS_BY_RESULT,
    DsarResultDTO,
    _result_dto,
)
from services import admin_data_privacy_service as admin_svc
from services import ban_suppression, dsar_outbox, tenant_service
from services import captcha_service as captcha
from services import dsar_crypto as crypto
from services import dsar_rate_limit as limiter
from services import dsar_service as svc
from services import dsar_telemetry as telemetry
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, not_found
from services.rate_limiting import _client_ip
from services.schema import bind_dsar_tables

logger = logging.getLogger(__name__)

SCOPE_TENANT_ADMIN = "tenant:admin"
_ACT_SUCCESS = (admin_svc.DsarStatus.COMPLETED, admin_svc.DsarStatus.ALREADY_DONE)

admin_dsar_bp = Blueprint("v1_tenant_dsar_inbox", __name__, url_prefix="/api/v1/tenant")
user_dsar_bp = Blueprint(
    "v1_user_dsar_requests", __name__, url_prefix="/api/v1/user/me/privacy-requests"
)
public_dsar_bp = Blueprint("v1_public_dsar_requests", __name__, url_prefix="/api/v1/public")

#: Public body cap (bytes): a form with an email, two codes, a token and a short message.
PUBLIC_MAX_BODY_BYTES = 8192
_SLUG_MAX = 63
#: (limit, window seconds) -- defaults; env-tunable would be a follow-up, kept explicit here.
#: Resource backstop on submit/resend per IP: a hard 429 about the CALLER's own traffic only (no
#: information about anyone else's email or tenant). Real abuse limits below are SOFT.
IP_HARD_LIMIT = (100, 3600)
VERIFY_IP_LIMIT = (30, 3600)
CHALLENGE_IP_LIMIT = (30, 3600)  # altcha PoW challenges: stops bulk pre-mining
CAPTCHA_CONFIG_IP_LIMIT = (120, 3600)
#: SOFT limits: beyond them the submission is silently suppressed (decoy work + the same 202), so
#: no 429 can reveal that an address was recently used or that a tenant exists.
IP_SOFT_LIMIT = (10, 3600)
EMAIL_LIMIT = (3, 3600)  # per (tenant, email fingerprint)
GLOBAL_EMAIL_LIMIT = (10, 86400)  # per email fingerprint across ALL tenants: no mail-bombing
RESEND_EMAIL_LIMIT = (3, 3600)
USER_LIMIT = (10, 86400)
#: Scope every signed-in session carries (viewer bundle); service tokens have none.
SCOPE_SELF_SERVICE = "*:read"
#: Generic, requester-visible reason shown for an extended request (no admin free text leaves).
EXTENSION_REASON_PUBLIC = "statutory_extension"


# ---------------------------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RequestDTO:
    """One inbox request. Enum fields are int codes (see `services/dsar_service.py`).

    `unverified` is true while a public-form requester has not proved their email: such rows have
    no `requesterEmail`/`requesterMessage` and no SLA clock (`dueAt`/`dueState` null). `dueState`
    is `ok` / `due_soon` / `overdue`, or null for closed and unverified requests.
    """

    id: str
    requestType: int
    status: int
    regime: int
    source: int
    requestedAt: str
    verifiedAt: str | None
    dueAt: str | None
    extendedUntil: str | None
    effectiveDueAt: str | None
    extensionReason: str | None
    dueState: str | None
    completedAt: str | None
    expiresAt: str | None
    assigneeId: str | None
    subjectUserId: str | None
    responseNotes: str | None
    requesterEmail: str | None
    requesterMessage: str | None
    contactPurged: bool
    unverified: bool


@dataclass(slots=True, frozen=True)
class RequestEventDTO:
    """One append-only audit event. `detail` is PII-free."""

    id: str
    eventType: int
    actorKind: int
    actorId: str | None
    fromStatus: int | None
    toStatus: int | None
    detail: dict[str, Any] | None
    createdAt: str


@dataclass(slots=True, frozen=True)
class RequestResponse:
    """Single-request envelope (create / update / extend)."""

    success: bool
    request: RequestDTO


@dataclass(slots=True, frozen=True)
class RequestDetailResponse:
    """`GET .../requests/<id>`: the request plus its full audit trail."""

    success: bool
    request: RequestDTO
    events: list[RequestEventDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class RequestListResponse:
    """Paginated inbox page. `total` counts every row matching the filters."""

    success: bool
    items: list[RequestDTO]
    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class ActResponse:
    """Result of executing the request's export / erase / do-not-sell."""

    success: bool
    request: RequestDTO
    result: DsarResultDTO


@dataclass(slots=True, frozen=True)
class CreateRequestBody:
    """Admin-entered request. `receivedAt` (ISO 8601) back-dates the SLA clock start."""

    requestType: int
    regime: int
    subjectUserId: str | None = None
    receivedAt: str | None = None
    requesterEmail: str | None = None
    notes: str | None = None


@dataclass(slots=True, frozen=True)
class UpdateRequestBody:
    """Partial update. `status` may only be 4, 5, 6 or 7. `clearAssignee` unassigns."""

    status: int | None = None
    assigneeId: str | None = None
    clearAssignee: bool = False
    responseNotes: str | None = None
    subjectUserId: str | None = None
    subjectOverrideReason: str | None = None


@dataclass(slots=True, frozen=True)
class ExtendBody:
    """The one allowed extension; `reason` is mandatory."""

    reason: str


@dataclass(slots=True, frozen=True)
class ActBody:
    """`action` must match the request type (1 export, 2 erase, 3 do_not_sell); erase needs confirm.

    `overrideReason` is required to erase on a PUBLIC-form request whose requester's verified email
    does not match the linked account's email; it is recorded in the event trail.
    """

    action: str
    confirm: bool = False
    overrideReason: str | None = None


@dataclass(slots=True, frozen=True)
class UserRequestBody:
    """Logged-in intake: what the caller asks for and under which regime."""

    requestType: int
    regime: int


@dataclass(slots=True, frozen=True)
class UserRequestDTO:
    """A caller's own request (no admin-only fields)."""

    id: str
    requestType: int
    status: int
    regime: int
    requestedAt: str
    dueAt: str | None
    completedAt: str | None
    extendedUntil: str | None = None
    extensionReason: str | None = None


@dataclass(slots=True, frozen=True)
class UserRequestResponse:
    """Envelope for `POST /api/v1/user/me/privacy-requests`."""

    success: bool
    request: UserRequestDTO


@dataclass(slots=True, frozen=True)
class UserRequestListResponse:
    """The caller's own requests, newest first."""

    success: bool
    items: list[UserRequestDTO]


@dataclass(slots=True, frozen=True)
class PublicSubmitBody:
    """Public form. No name field by design. `captchaToken` is the provider's response token."""

    email: str
    requestType: int
    regime: int
    captchaToken: str
    message: str | None = None


@dataclass(slots=True, frozen=True)
class PublicResendBody:
    """Re-send the verification email for the pending request of `requestType` (captcha required)."""

    email: str
    requestType: int
    captchaToken: str


@dataclass(slots=True, frozen=True)
class PublicAcceptedResponse:
    """Uniform 202 body: identical whether or not the tenant/email exist or a row was created."""

    success: bool
    accepted: bool


@dataclass(slots=True, frozen=True)
class VerifyBody:
    """Verification token from the emailed link."""

    token: str


@dataclass(slots=True, frozen=True)
class VerifyPreviewResponse:
    """Non-consuming confirm-page data: what the link would verify. No PII, no tenant internals."""

    success: bool
    valid: bool
    requestType: int
    regime: int
    expiresAt: str | None


@dataclass(slots=True, frozen=True)
class VerifyResponse:
    """`verified` is always true on 200; any failure is the same 400 `DSAR_VERIFICATION_INVALID`."""

    success: bool
    verified: bool


@dataclass(slots=True, frozen=True)
class CaptchaConfigResponse:
    """Everything the browser needs; never a secret. `challengeUrl` is set for altcha only."""

    success: bool
    provider: str
    siteKey: str | None
    challengeUrl: str | None


@dataclass(slots=True, frozen=True)
class AltchaChallengeResponse:
    """Signed altcha proof-of-work challenge (self-hosted captcha)."""

    success: bool
    algorithm: str
    challenge: str
    salt: str
    signature: str
    maxnumber: int


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: datetime | None) -> str | None:
    moment = svc.aware(value)
    return moment.isoformat() if moment else None


def _hops() -> int:
    cfg = current_app.config.get("HUB_API_CONFIG")
    return int(getattr(cfg, "trusted_proxy_hops", 0) or 0)


def _inbox_disabled() -> ApiError:
    return ApiError("The DSAR inbox is not enabled", 404, "DSAR_INBOX_DISABLED")


def _rate_limited(scope: str) -> ApiError:
    telemetry.rate_limited.add(1, {"scope": scope})
    logger.info("dsar.rate_limited", extra={"scope": scope})
    return ApiError("Too many requests, try again later", 429, "DSAR_RATE_LIMITED")


async def _build_dto(async_dal: Any, dal: Any, rows: list[Any], now: datetime) -> list[RequestDTO]:
    """Rows -> DTOs with one batched user-uuid lookup; contact data only when revealable."""
    ids = {int(r.assignee_user_id) for r in rows if r.assignee_user_id}
    ids |= {int(r.subject_user_id) for r in rows if r.subject_user_id}
    uuids = await svc.user_uuids(async_dal, dal, ids)
    out: list[RequestDTO] = []
    for r in rows:
        email, message = svc.decrypt_contact(r)
        status = svc.RequestStatus(r.status)
        out.append(
            RequestDTO(
                id=str(r.public_id),
                requestType=int(r.request_type),
                status=int(r.status),
                regime=int(r.regime),
                source=int(r.source),
                requestedAt=_iso(r.requested_at) or "",
                verifiedAt=_iso(r.verified_at),
                dueAt=_iso(r.due_at),
                extendedUntil=_iso(r.extended_until),
                effectiveDueAt=_iso(svc.effective_due(r.due_at, r.extended_until)),
                extensionReason=r.extension_reason,
                dueState=svc.compute_due_state(r.due_at, r.extended_until, int(r.status), now),
                completedAt=_iso(r.completed_at),
                expiresAt=_iso(r.expires_at),
                assigneeId=uuids.get(int(r.assignee_user_id)) if r.assignee_user_id else None,
                subjectUserId=uuids.get(int(r.subject_user_id)) if r.subject_user_id else None,
                responseNotes=r.response_notes,
                requesterEmail=email,
                requesterMessage=message,
                contactPurged=r.contact_purged_at is not None,
                unverified=status is svc.RequestStatus.VERIFICATION_PENDING,
            )
        )
    return out


def _user_dto(r: Any) -> UserRequestDTO:
    return UserRequestDTO(
        id=str(r.public_id),
        requestType=int(r.request_type),
        status=int(r.status),
        regime=int(r.regime),
        requestedAt=_iso(r.requested_at) or "",
        dueAt=_iso(r.due_at),
        completedAt=_iso(r.completed_at),
        extendedUntil=_iso(r.extended_until),
        extensionReason=EXTENSION_REASON_PUBLIC if r.extended_until is not None else None,
    )


async def _admin_ctx(tenant_slug: str) -> tuple[admin_svc.DsarActor, int]:
    """Tenant -> slug-match -> inbox-rollout-flag. NOT tier-gated (statutory)."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware postcondition
    tenant_service.require_matching_tenant(tenant_slug, ctx.tenant_slug)
    if not await feature_enabled(svc.FEATURE_DSAR_INBOX, tenant=ctx.tenant_slug):
        raise _inbox_disabled()
    actor = admin_svc.DsarActor(
        user_id=get_current_user_id(request),
        tenant_id=int(ctx.tenant_id),
        tenant_slug=ctx.tenant_slug,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )
    return actor, int(ctx.tenant_id)


def _parse_list_filters() -> svc.ListFilters:
    """Strict query parsing: any malformed/unknown value is a 400, never silently ignored."""
    args = request.args
    unknown = set(args) - {
        "status",
        "type",
        "regime",
        "dueState",
        "assignee",
        "sort",
        "order",
        "limit",
        "offset",
    }
    if unknown:
        raise bad_request(f"Unknown query parameter: {sorted(unknown)[0]}")

    def _int(name: str, enum_cls: Any = None) -> int | None:
        raw = args.get(name)
        if raw is None:
            return None
        if not raw.isascii() or not raw.isdigit():
            raise bad_request(f"{name} must be an integer code")
        value = int(raw)
        if enum_cls is not None:
            svc.parse_enum(enum_cls, value, name)
        return value

    due_state = args.get("dueState")
    if due_state is not None and due_state not in svc.DUE_STATES:
        raise bad_request(f"dueState must be one of: {', '.join(svc.DUE_STATES)}")
    sort = args.get("sort", "requestedAt")
    if sort not in ("requestedAt", "dueAt"):
        raise bad_request("sort must be requestedAt or dueAt")
    order = args.get("order", "desc")
    if order not in ("asc", "desc"):
        raise bad_request("order must be asc or desc")
    limit = _int("limit")
    offset = _int("offset")
    if limit is not None and not 1 <= limit <= 100:
        raise bad_request("limit must be between 1 and 100")
    return svc.ListFilters(
        status=_int("status", svc.RequestStatus),
        request_type=_int("type", svc.RequestType),
        regime=_int("regime", svc.Regime),
        due_state=due_state,
        sort=sort,
        descending=order == "desc",
        limit=limit if limit is not None else 25,
        offset=offset if offset is not None else 0,
    )


async def _assignee_filter(
    async_dal: Any, dal: Any, tenant_id: int, filters: svc.ListFilters
) -> svc.ListFilters:
    """Resolve `?assignee=<uuid>` to an internal id (400 on a malformed uuid).

    Only an admin of THIS tenant can be an assignee, so an unknown, foreign-tenant or non-admin
    uuid all resolve identically to "matches nothing" (id 0 never exists): the filter never
    reveals whether a uuid exists elsewhere on the platform.
    """
    raw = request.args.get("assignee")
    if raw is None:
        return filters
    svc.parse_uuid(raw, "user id")
    user_id = await svc.tenant_admin_id_for_uuid(async_dal, dal, tenant_id, raw)
    return dataclasses.replace(filters, assignee_user_id=user_id if user_id else 0)


# ---------------------------------------------------------------------------------------------
# Admin inbox
# ---------------------------------------------------------------------------------------------


@admin_dsar_bp.before_request
async def _ensure_dsar_tables() -> None:
    """Idempotently bind the DSAR tables (same pattern as the other privacy blueprints)."""
    bind_dsar_tables(current_app.config["dal"])


@admin_dsar_bp.route("/<tenant_slug>/privacy/requests", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@document_response(RequestListResponse)
async def list_dsar_requests(tenant_slug: str) -> tuple[Any, int]:
    """List the tenant's DSAR inbox. Unverified/expired requests only appear when `status` asks."""
    async_dal, dal = _dal()
    try:
        _, tenant_id = await _admin_ctx(tenant_slug)
        filters = await _assignee_filter(async_dal, dal, tenant_id, _parse_list_filters())
        now = svc.now_utc()
        await svc.run_maintenance(async_dal, dal, now=now)
        rows, total = await svc.list_requests(
            async_dal, dal, tenant_id=tenant_id, filters=filters, now=now
        )
        items = await _build_dto(async_dal, dal, rows, now)
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(
        RequestListResponse(
            success=True, items=items, total=total, limit=filters.limit, offset=filters.offset
        )
    )


@admin_dsar_bp.route("/<tenant_slug>/privacy/requests", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@validate_request(CreateRequestBody)
@document_response(RequestResponse)
async def create_dsar_request(data: CreateRequestBody, tenant_slug: str) -> tuple[Any, int]:
    """Enter a request received out-of-band (letter, email, phone). Status starts `received`."""
    async_dal, dal = _dal()
    try:
        actor, tenant_id = await _admin_ctx(tenant_slug)
        req_type = svc.parse_enum(svc.RequestType, data.requestType, "requestType")
        regime = svc.parse_enum(svc.Regime, data.regime, "regime")
        email = svc.validate_email(data.requesterEmail) if data.requesterEmail else None
        if email is not None and not crypto.is_configured():
            raise ApiError("Contact storage is not configured", 503, "DSAR_UNAVAILABLE")
        received = None
        if data.receivedAt:
            try:
                received = datetime.fromisoformat(data.receivedAt)
            except ValueError as exc:
                raise bad_request("receivedAt must be an ISO 8601 timestamp") from exc
            received = svc.check_backdate(received)
        subject_id = None
        if data.subjectUserId:
            subject_id = await svc.user_id_for_uuid_in_tenant(
                async_dal, dal, tenant_id, data.subjectUserId
            )
            if subject_id is None:
                raise svc.subject_not_found()
        notes = svc.clean_text(data.notes, "notes", svc.NOTES_MAX)
        row, _ = await svc.create_request(
            async_dal,
            dal,
            tenant_id=tenant_id,
            request_type=req_type,
            regime=regime,
            source=svc.Source.ADMIN_ENTERED,
            actor_kind=svc.ActorKind.ADMIN,
            actor_user_id=actor.user_id,
            subject_user_id=subject_id,
            requested_at=received,
            email=email,
            notes=notes,
        )
        dto = (await _build_dto(async_dal, dal, [row], svc.now_utc()))[0]
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(RequestResponse(success=True, request=dto), 201)


async def _load(async_dal: Any, dal: Any, tenant_id: int, request_id: str) -> Any:
    row = await svc.get_request(async_dal, dal, tenant_id=tenant_id, public_id=request_id)
    if row is None:
        raise not_found("Request not found")
    return row


@admin_dsar_bp.route("/<tenant_slug>/privacy/requests/<request_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@document_response(RequestDetailResponse)
async def get_dsar_request(tenant_slug: str, request_id: str) -> tuple[Any, int]:
    """One request with its audit trail. Another tenant's id is a 404, never a 403."""
    async_dal, dal = _dal()
    try:
        _, tenant_id = await _admin_ctx(tenant_slug)
        row = await _load(async_dal, dal, tenant_id, request_id)
        events = await svc.load_events(async_dal, dal, tenant_id=tenant_id, request_id=int(row.id))
        uuids = await svc.user_uuids(
            async_dal, dal, {int(e.actor_user_id) for e in events if e.actor_user_id}
        )
        dto = (await _build_dto(async_dal, dal, [row], svc.now_utc()))[0]
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(
        RequestDetailResponse(
            success=True,
            request=dto,
            events=[
                RequestEventDTO(
                    id=str(e.public_id),
                    eventType=int(e.event_type),
                    actorKind=int(e.actor_kind),
                    actorId=uuids.get(int(e.actor_user_id)) if e.actor_user_id else None,
                    fromStatus=e.from_status,
                    toStatus=e.to_status,
                    detail=e.detail,
                    createdAt=_iso(e.created_at) or "",
                )
                for e in events
            ],
        )
    )


@admin_dsar_bp.route("/<tenant_slug>/privacy/requests/<request_id>", methods=["PATCH"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@validate_request(UpdateRequestBody)
@document_response(RequestResponse)
async def update_dsar_request(
    data: UpdateRequestBody, tenant_slug: str, request_id: str
) -> tuple[Any, int]:
    """Change status (4-7), assignee, notes or linked subject; 409 while the requester is unverified."""
    async_dal, dal = _dal()
    try:
        actor, tenant_id = await _admin_ctx(tenant_slug)
        row = await _load(async_dal, dal, tenant_id, request_id)
        assignee_id = None
        if data.assigneeId:
            assignee_id = await svc.tenant_admin_id_for_uuid(
                async_dal, dal, tenant_id, data.assigneeId
            )
            if assignee_id is None:
                raise svc.assignee_invalid()
        subject_id = None
        if data.subjectUserId:
            subject_id = await svc.user_id_for_uuid_in_tenant(
                async_dal, dal, tenant_id, data.subjectUserId
            )
            if subject_id is None:
                raise svc.subject_not_found()
        notes = svc.clean_text(data.responseNotes, "responseNotes", svc.NOTES_MAX)
        if data.status is not None:
            svc.parse_enum(svc.RequestStatus, data.status, "status")
        updated = await svc.update_request(
            async_dal,
            dal,
            tenant_id=tenant_id,
            row=row,
            admin_user_id=actor.user_id,
            status=data.status,
            assignee_user_id=assignee_id,
            clear_assignee=data.clearAssignee,
            notes=notes,
            notes_provided=data.responseNotes is not None,
            subject_user_id=subject_id,
            subject_override_reason=data.subjectOverrideReason,
        )
        dto = (await _build_dto(async_dal, dal, [updated], svc.now_utc()))[0]
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(RequestResponse(success=True, request=dto))


@admin_dsar_bp.route("/<tenant_slug>/privacy/requests/<request_id>/extend", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@validate_request(ExtendBody)
@document_response(RequestResponse)
async def extend_dsar_request(
    data: ExtendBody, tenant_slug: str, request_id: str
) -> tuple[Any, int]:
    """Use the single allowed extension (GDPR +2 calendar months, CCPA +45d, other +30d); a reason is required."""
    async_dal, dal = _dal()
    try:
        actor, tenant_id = await _admin_ctx(tenant_slug)
        row = await _load(async_dal, dal, tenant_id, request_id)
        updated = await svc.extend_request(
            async_dal,
            dal,
            tenant_id=tenant_id,
            row=row,
            admin_user_id=actor.user_id,
            reason=data.reason,
        )
        dto = (await _build_dto(async_dal, dal, [updated], svc.now_utc()))[0]
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(RequestResponse(success=True, request=dto))


@admin_dsar_bp.route("/<tenant_slug>/privacy/requests/<request_id>/act", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@validate_request(ActBody)
@document_response(ActResponse)
async def act_on_dsar_request(data: ActBody, tenant_slug: str, request_id: str) -> tuple[Any, int]:
    """Execute export / erase / do-not-sell for the request's subject (reuses the admin DSAR logic).

    409 `DSAR_UNVERIFIED` until a public requester verified their email. Not tier-gated.
    """
    async_dal, dal = _dal()
    try:
        actor, tenant_id = await _admin_ctx(tenant_slug)
        row = await _load(async_dal, dal, tenant_id, request_id)
        svc.require_actionable(row)
        expected = svc.ACT_ACTION_FOR_TYPE.get(svc.RequestType(row.request_type))
        if expected is None:
            raise ApiError(
                "This request type is handled manually", 422, "DSAR_ACTION_NOT_APPLICABLE"
            )
        if data.action != expected:
            raise ApiError(
                f"This request type only supports action '{expected}'",
                422,
                "DSAR_ACTION_NOT_APPLICABLE",
            )
        if row.subject_user_id is None:
            raise ApiError(
                "The request has no linked user; set subjectUserId first",
                422,
                "DSAR_NO_SUBJECT_USER",
            )
        action = admin_svc.DsarAction(expected)
        subject_id = int(row.subject_user_id)
        override_detail: dict[str, Any] = {}
        if (
            action is admin_svc.DsarAction.ERASE
            and svc.Source(row.source) is svc.Source.PUBLIC_FORM
            and not await svc.subject_email_matches(
                async_dal, dal, subject_user_id=subject_id, row=row
            )
        ):
            # An unauthenticated requester only proved control of THEIR mailbox: erasing a linked
            # account needs that mailbox to be the account's, or a recorded admin override.
            reason = svc.clean_text(data.overrideReason, "overrideReason", svc.REASON_MAX)
            if not reason:
                raise ApiError(
                    "The requester's verified email does not match the linked account; "
                    "overrideReason is required",
                    422,
                    "DSAR_SUBJECT_EMAIL_MISMATCH",
                )
            override_detail = {"email_override": True, "override_reason": reason}
        claimed = await svc.claim_for_action(async_dal, dal, tenant_id=tenant_id, row=row)
        refusal_reason: str | None = None
        if await svc.subject_in_tenant(async_dal, dal, tenant_id, subject_id):
            result = await admin_svc.run_single_dsar(
                async_dal,
                dal,
                actor=actor,
                action=action,
                user_id=subject_id,
                confirm=data.confirm,
            )
        else:
            # Defence in depth: the subject left the tenant after being linked. Uniform
            # not-found to the client; the true reason lives in the audit + request event.
            subject_uuid = (await svc.user_uuids(async_dal, dal, {subject_id})).get(
                subject_id, svc.NIL_UUID
            )
            result = await admin_svc.deny_out_of_tenant(
                async_dal, dal, actor=actor, action=action, user_uuid=subject_uuid
            )
            refusal_reason = "subject_not_in_tenant"
        # ONLY a fully successful result may complete a request; `partial` keeps it in progress.
        succeeded = result.status in _ACT_SUCCESS
        updated = await svc.mark_acted(
            async_dal,
            dal,
            tenant_id=tenant_id,
            row=claimed,
            admin_user_id=actor.user_id,
            action=expected,
            outcome=result.status.value,
            succeeded=succeeded,
            partial=result.status is admin_svc.DsarStatus.PARTIAL,
            reason=refusal_reason,
            detail=override_detail or None,
        )
        dto = (await _build_dto(async_dal, dal, [updated], svc.now_utc()))[0]
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    status = _HTTP_STATUS_BY_RESULT[result.status]
    response, _ = jsonify_dto(
        ActResponse(success=succeeded, request=dto, result=_result_dto(result)), status
    )
    return response, status


# ---------------------------------------------------------------------------------------------
# Logged-in intake
# ---------------------------------------------------------------------------------------------


@user_dsar_bp.before_request
async def _ensure_user_tables() -> None:
    """Idempotently bind the DSAR tables."""
    bind_dsar_tables(current_app.config["dal"])


def _caller_id() -> int:
    """The caller's `hub_users.id`; anything but a positive id is a 401.

    Guarantees a zero/negative/absent id can never be compared against NULL subject rows.
    """
    user_id = get_current_user_id(request)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ApiError("Authentication required", 401, "UNAUTHORIZED")
    return user_id


@user_dsar_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_SELF_SERVICE)  # type: ignore[misc]
@validate_request(UserRequestBody)
@document_response(UserRequestResponse)
async def create_my_dsar_request(data: UserRequestBody) -> tuple[Any, int]:
    """File a data-subject request as the signed-in user (tenant from the token, never the body)."""
    async_dal, dal = _dal()
    try:
        user_id = _caller_id()
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 - tenant_middleware postcondition
        if not await feature_enabled(svc.FEATURE_DSAR_INBOX, tenant=ctx.tenant_slug):
            raise _inbox_disabled()
        req_type = svc.parse_enum(svc.RequestType, data.requestType, "requestType")
        regime = svc.parse_enum(svc.Regime, data.regime, "regime")
        if not await limiter.allow(f"user:{ctx.tenant_id}:{user_id}", *USER_LIMIT):
            raise _rate_limited("user")
        if await svc.find_open_duplicate(
            async_dal,
            dal,
            tenant_id=int(ctx.tenant_id),
            subject_user_id=user_id,
            request_type=req_type,
        ):
            raise ApiError(
                "You already have an open request of this type", 409, "DSAR_DUPLICATE_OPEN"
            )
        row, _ = await svc.create_request(
            async_dal,
            dal,
            tenant_id=int(ctx.tenant_id),
            request_type=req_type,
            regime=regime,
            source=svc.Source.LOGGED_IN_USER,
            actor_kind=svc.ActorKind.SUBJECT,
            actor_user_id=user_id,
            subject_user_id=user_id,
        )
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(UserRequestResponse(success=True, request=_user_dto(row)), 201)


@user_dsar_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_SELF_SERVICE)  # type: ignore[misc]
@document_response(UserRequestListResponse)
async def list_my_dsar_requests() -> tuple[Any, int]:
    """The caller's own requests in this tenant, newest first."""
    async_dal, dal = _dal()
    try:
        user_id = _caller_id()
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 - tenant_middleware postcondition
        if not await feature_enabled(svc.FEATURE_DSAR_INBOX, tenant=ctx.tenant_slug):
            raise _inbox_disabled()
        rows = await async_dal.select_async(
            dal(
                (dal.dsar_requests.tenant_id == int(ctx.tenant_id))
                & (dal.dsar_requests.subject_user_id == user_id)
            ),
            orderby=~dal.dsar_requests.id,
            limitby=(0, 100),
        )
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return jsonify_dto(UserRequestListResponse(success=True, items=[_user_dto(r) for r in rows]))


# ---------------------------------------------------------------------------------------------
# Public form
# ---------------------------------------------------------------------------------------------

_PUBLIC_RATE_LIMITED_ENDPOINTS = {
    "v1_public_dsar_requests.submit_public_dsar_request": IP_HARD_LIMIT,
    "v1_public_dsar_requests.resend_dsar_verification": IP_HARD_LIMIT,
    "v1_public_dsar_requests.verify_dsar_get": VERIFY_IP_LIMIT,
    "v1_public_dsar_requests.verify_dsar_post": VERIFY_IP_LIMIT,
    "v1_public_dsar_requests.verify_dsar_preview": VERIFY_IP_LIMIT,
    "v1_public_dsar_requests.get_dsar_altcha_challenge": CHALLENGE_IP_LIMIT,
    "v1_public_dsar_requests.get_dsar_captcha_config": CAPTCHA_CONFIG_IP_LIMIT,
}


@public_dsar_bp.before_request
async def _public_guard() -> tuple[dict[str, object], int] | None:
    """Bind tables, cap the body, and apply the per-IP limit before any parsing happens."""
    endpoint = request.endpoint or ""
    if not endpoint.startswith("v1_public_dsar_requests."):
        return None
    bind_dsar_tables(current_app.config["dal"])
    if request.method == "POST":
        length = request.content_length
        if length is not None and length > PUBLIC_MAX_BODY_BYTES:
            return _err(ApiError("Request body too large", 413, "PAYLOAD_TOO_LARGE"))
        body = getattr(request, "body", None)
        if body is not None and hasattr(body, "_max_content_length"):
            body._max_content_length = PUBLIC_MAX_BODY_BYTES  # chunked uploads: cap while streaming
    limit = _PUBLIC_RATE_LIMITED_ENDPOINTS.get(endpoint)
    if limit is not None:
        try:
            within = await limiter.allow(f"ip:{endpoint}:{_client_ip(_hops())}", *limit)
        except ApiError as exc:  # shared store down -> uniform 503, never fail open
            logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
            return _err(exc)
        if not within:
            return _err(_rate_limited("ip"))
    return None


def _slug_ok(slug: str) -> bool:
    return (
        0 < len(slug) <= _SLUG_MAX and slug.isascii() and all(c.isalnum() or c == "-" for c in slug)
    )


async def _replay_guard(challenge: str, ttl: int) -> bool:
    """Altcha single use: first sight wins (atomic SET NX EX on the shared store); fails closed."""
    return await limiter.once(f"altcha:{challenge}", ttl)


async def _require_captcha(token: str) -> None:
    """Fail closed (503) when unconfigured/unreachable, 400 when the token is wrong."""
    if not crypto.is_configured():
        logger.error("dsar.public_unavailable_no_encryption_key")
        raise ApiError("The request form is temporarily unavailable", 503, "DSAR_UNAVAILABLE")
    if captcha.provider() is None:
        logger.error("dsar.public_unavailable_no_captcha")
        raise ApiError(
            "The request form is temporarily unavailable", 503, "DSAR_CAPTCHA_UNAVAILABLE"
        )
    try:
        await captcha.verify(token, remote_ip=_client_ip(_hops()), replay_guard=_replay_guard)
    except captcha.CaptchaUnavailable as exc:
        raise ApiError(
            "The request form is temporarily unavailable", 503, "DSAR_CAPTCHA_UNAVAILABLE"
        ) from exc
    except captcha.CaptchaRejected as exc:
        raise ApiError("Captcha verification failed", 400, "DSAR_CAPTCHA_FAILED") from exc


def _accepted() -> tuple[Any, int]:
    return jsonify_dto(PublicAcceptedResponse(success=True, accepted=True), 202)


async def _resolve_public_tenant(async_dal: Any, dal: Any, slug: str) -> int | None:
    """Active tenant id for the URL slug when the inbox is enabled for it; else `None` (silent)."""
    if not _slug_ok(slug):
        return None
    rows = await async_dal.select_async(
        dal((dal.tenants.slug == slug.lower()) & (dal.tenants.is_active == True)),  # noqa: E712
        dal.tenants.id,
    )
    if not rows:
        return None
    if not await feature_enabled(svc.FEATURE_DSAR_INBOX, tenant=slug.lower()):
        return None
    return int(rows[0].id)


@public_dsar_bp.route("/privacy-requests/captcha-config", methods=["GET"])
@document_response(CaptchaConfigResponse)
async def get_dsar_captcha_config() -> tuple[Any, int]:
    """Provider name + public site key (+ altcha URL); 503 when no provider is configured."""
    try:
        config = captcha.public_config()
    except captcha.CaptchaUnavailable:
        logger.warning("dsar.captcha.unavailable provider_not_configured")
        return _err(
            ApiError("The request form is temporarily unavailable", 503, "DSAR_CAPTCHA_UNAVAILABLE")
        )
    return jsonify_dto(
        CaptchaConfigResponse(
            success=True,
            provider=str(config["provider"]),
            siteKey=config["siteKey"],
            challengeUrl=config["challengeUrl"],
        )
    )


@public_dsar_bp.route("/privacy-requests/altcha-challenge", methods=["GET"])
@document_response(AltchaChallengeResponse)
async def get_dsar_altcha_challenge() -> tuple[Any, int]:
    """Issue a signed altcha challenge (only when altcha is the configured provider)."""
    try:
        issued = captcha.create_altcha_challenge()
    except captcha.CaptchaUnavailable:
        logger.warning("dsar.captcha.unavailable provider_not_configured")
        return _err(
            ApiError("The request form is temporarily unavailable", 503, "DSAR_CAPTCHA_UNAVAILABLE")
        )
    return jsonify_dto(AltchaChallengeResponse(success=True, **issued))


@public_dsar_bp.after_request
async def _public_no_store(response: Any) -> Any:
    """Verification responses are never cached nor leak a Referer (the confirm page holds a token)."""
    endpoint = request.endpoint or ""
    if endpoint.startswith("v1_public_dsar_requests.verify_"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def _soft_limits_allow(
    slug: str, fingerprint: str, scope: str, email_limit: tuple[int, int]
) -> bool:
    """Evaluate EVERY soft abuse limit (no short-circuit: uniform cost) -> True only if all allow.

    Per-IP, per-(tenant, email) and GLOBAL per-email. Breaching one never produces an error: the
    caller suppresses the send and returns the same 202 (no 429 oracle about recent activity).
    Backend outage still fails closed (503) via the shared limiter.
    """
    slug_key = slug.lower()[:_SLUG_MAX]
    verdicts = [
        await limiter.allow(f"ip-soft:{scope}:{_client_ip(_hops())}", *IP_SOFT_LIMIT),
        await limiter.allow(f"pub-email-global:{fingerprint}", *GLOBAL_EMAIL_LIMIT),
        await limiter.allow(f"{scope}:{slug_key}:{fingerprint}", *email_limit),
    ]
    return all(verdicts)


async def _suppress(reason: str, writes: int) -> tuple[Any, int]:
    """Silently drop a send: same DB work as a real enqueue, same 202 (nothing reveals why)."""
    telemetry.rate_limited.add(1, {"scope": "suppressed"})
    logger.debug("dsar.public_suppressed", extra={"reason": reason})
    async_dal, dal = _dal()
    await dsar_outbox.decoy(async_dal, dal, writes=writes)
    return _accepted()


async def _intake_public(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    slug: str,
    email: str,
    req_type: svc.RequestType,
    regime: svc.Regime,
    message: str | None,
) -> None:
    """ONE transaction: the request write (or token rotation) AND its outbox mail row.

    An open request of the same type for this email (a pre-submitted one, a double click, a lost
    race against the partial unique index) is re-verified instead of duplicated: its token is
    rotated and the mail goes to the email's owner. A crash can therefore never leave a request
    without its mail, nor a mail without its request.
    """
    created = False
    for attempt in (1, 2):

        def work() -> bool:
            rotated = svc.rotate_pending_public_sync(
                dal, tenant_id=tenant_id, email=email, request_type=req_type
            )
            fresh = rotated is None
            if rotated is None:
                row, token = svc.create_public_request_sync(
                    dal,
                    tenant_id=tenant_id,
                    request_type=req_type,
                    regime=regime,
                    email=email,
                    message=message,
                )
            else:
                row, token = rotated
            dsar_outbox.enqueue_in_transaction(
                dal, request_row=row, token=token, resend=not fresh, tenant_slug=slug
            )
            return fresh

        try:
            created = await ban_suppression.run_in_transaction(async_dal, dal, work)
        except Exception as exc:
            if attempt == 1 and svc.is_integrity_violation(exc):
                logger.debug("dsar.public_submit_race_retry")
                continue  # lost the unique-index race: the winner's row is there to rotate now
            raise
        break
    dsar_outbox.after_commit_enqueued()
    if created:
        svc.note_request_created(svc.Source.PUBLIC_FORM, req_type, tenant_id)


@public_dsar_bp.route("/<tenant_slug>/privacy-requests", methods=["POST"])
@validate_request(PublicSubmitBody)
@document_response(PublicAcceptedResponse)
async def submit_public_dsar_request(data: PublicSubmitBody, tenant_slug: str) -> tuple[Any, int]:
    """Public DSAR form. Captcha + email verification required; uniform 202 for every valid submit.

    Never reveals whether the tenant or the email exists or whether a limit was hit. The request
    is stored `verification_pending` (not actionable, no SLA clock) until the emailed token is
    consumed; request row and mail row commit atomically.
    """
    async_dal, dal = _dal()
    try:
        req_type = svc.parse_enum(svc.RequestType, data.requestType, "requestType")
        regime = svc.parse_enum(svc.Regime, data.regime, "regime")
        email = svc.validate_email(data.email)
        message = svc.clean_text(data.message, "message", svc.MESSAGE_MAX)
        await _require_captcha(data.captchaToken)
        fingerprint = crypto.email_fingerprint(email).hex()
        within = await _soft_limits_allow(tenant_slug, fingerprint, "pub-email", EMAIL_LIMIT)
        tenant_id = await _resolve_public_tenant(async_dal, dal, tenant_slug)
        if not within:
            return await _suppress("soft_limit", 3)
        if tenant_id is None:
            logger.debug("dsar.public_submit_dropped", extra={"reason": "tenant_or_flag"})
            await dsar_outbox.decoy(async_dal, dal, writes=3)
            return _accepted()
        await _intake_public(
            async_dal,
            dal,
            tenant_id=tenant_id,
            slug=tenant_slug,
            email=email,
            req_type=req_type,
            regime=regime,
            message=message,
        )
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return _accepted()


@public_dsar_bp.route("/<tenant_slug>/privacy-requests/resend-verification", methods=["POST"])
@validate_request(PublicResendBody)
@document_response(PublicAcceptedResponse)
async def resend_dsar_verification(data: PublicResendBody, tenant_slug: str) -> tuple[Any, int]:
    """Re-send the verification email for the pending request of `requestType` (uniform 202).

    Only the pending request of the SAME type is rotated, so a link can never verify a request the
    caller did not ask about. Identical response AND timing whether or not one exists (the
    no-pending branch spends the same DB work via `dsar_outbox.decoy`); mail is queued with the
    token rotation in ONE transaction, never awaited. The previous token stops working.
    """
    async_dal, dal = _dal()
    try:
        req_type = svc.parse_enum(svc.RequestType, data.requestType, "requestType")
        email = svc.validate_email(data.email)
        await _require_captcha(data.captchaToken)
        fingerprint = crypto.email_fingerprint(email).hex()
        within = await _soft_limits_allow(
            tenant_slug, fingerprint, "pub-resend", RESEND_EMAIL_LIMIT
        )
        tenant_id = await _resolve_public_tenant(async_dal, dal, tenant_slug)
        if not within:
            return await _suppress("soft_limit", 2)
        if tenant_id is None:
            await dsar_outbox.decoy(async_dal, dal, writes=2)
            return _accepted()

        def work() -> bool:
            rotated = svc.rotate_pending_public_sync(
                dal, tenant_id=tenant_id, email=email, request_type=req_type
            )
            if rotated is None:
                return False
            row, token = rotated
            dsar_outbox.enqueue_in_transaction(
                dal, request_row=row, token=token, resend=True, tenant_slug=tenant_slug
            )
            return True

        if await ban_suppression.run_in_transaction(async_dal, dal, work):
            dsar_outbox.after_commit_enqueued()
        else:
            await dsar_outbox.decoy(async_dal, dal, writes=2)
    except ApiError as exc:
        logger.debug("DSAR.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return _accepted()


def _verification_invalid() -> tuple[Any, int]:
    """The ONE failure for an unknown / expired / used / malformed token (400)."""
    return _err(
        ApiError(
            "The verification link is invalid or has expired", 400, "DSAR_VERIFICATION_INVALID"
        )
    )


async def _preview(token: str) -> tuple[Any, int]:
    """Non-consuming: the type/regime/expiry the token would verify, or the uniform 400."""
    async_dal, dal = _dal()
    row = await svc.preview_verification_token(async_dal, dal, token)
    if row is None:
        return _verification_invalid()
    return jsonify_dto(
        VerifyPreviewResponse(
            success=True,
            valid=True,
            requestType=int(row.request_type),
            regime=int(row.regime),
            expiresAt=_iso(row.verification_expires_at),
        )
    )


@public_dsar_bp.route("/privacy-requests/verify", methods=["GET"])
@document_response(VerifyPreviewResponse)
async def verify_dsar_get() -> tuple[Any, int]:
    """NON-consuming preview of `?token=` (mail scanners prefetch GET links; only POST consumes).

    Prefer `POST /privacy-requests/verify/preview`: a token in a query string can land in proxy
    access logs, a body cannot.
    """
    return await _preview(request.args.get("token", ""))


@public_dsar_bp.route("/privacy-requests/verify/preview", methods=["POST"])
@validate_request(VerifyBody)
@document_response(VerifyPreviewResponse)
async def verify_dsar_preview(data: VerifyBody) -> tuple[Any, int]:
    """Same preview as the GET with the token in the body (never in a URL or access log)."""
    return await _preview(data.token)


@public_dsar_bp.route("/privacy-requests/verify", methods=["POST"])
@validate_request(VerifyBody)
@document_response(VerifyResponse)
async def verify_dsar_post(data: VerifyBody) -> tuple[Any, int]:
    """Consume the single-use verification token; moves the request to `verified`."""
    async_dal, dal = _dal()
    if await svc.consume_verification_token(async_dal, dal, data.token):
        return jsonify_dto(VerifyResponse(success=True, verified=True))
    return _verification_invalid()


BLUEPRINTS: list[Blueprint] = [admin_dsar_bp, user_dsar_bp, public_dsar_bp]
