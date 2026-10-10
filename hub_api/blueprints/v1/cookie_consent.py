"""v1 `cookie consent` (GDPR/CCPA) group -- ported from `cookieConsentController.js`.

Mounted at the exact Node path, `/api/v1/cookie` (`routes/
cookieConsent.js`, via `routes/index.js`'s `router.use('/cookie',
cookieConsentRoutes)`). Auth pattern matches `hub_api/PORTING.md`'s
table exactly:

- ``GET/POST ""``, ``GET policy``, ``GET policy/history`` -- no
  decorators. Pre-auth: anonymous visitors, resolved via the
  ``waddlebot_consent_id`` cookie, are normal callers for these routes in
  Node too (`routes/cookieConsent.js` mounts them with no `requireAuth`).
- ``PATCH preferences``, ``DELETE ""``, ``GET audit`` -- `@tenant_
  middleware` only. Self-service: subject resolved from the bearer JWT
  via `services.current_user.get_current_user_id`, never a request
  parameter.
- ``POST policy``, ``PUT policy/<version>/activate`` -- `@tenant_
  middleware` + `@require_scope("settings:write")`. Node's
  `requireSuperAdmin`; `settings:write` is in both the global and tenant
  `admin` `SCOPE_BUNDLES` bundle (`flask_core.auth`), the same scope
  `user_management.py`'s super-admin-gated group maps its own
  `requireSuperAdmin` routes to via `users:admin` (a sibling entry in the
  same bundle).

`@validate_response` vs `jsonify_dto()`: per `hub_api/PORTING.md`
Gotcha #3, a route that awaits `insert_async()` and then returns a
NESTED-dataclass response crashes; select-only or flat-response routes
are confirmed safe. `get_consent`/`get_current_policy`/`get_policy_
history`/`get_audit_log` are select-only (safe with `@validate_response`
despite a nested `data` field); `save_consent`/`update_preferences`/
`revoke_consent`/`create_policy_version` all call `insert_async` (either
the main write or `log_audit_event`'s own insert) and return a nested
`data` field, so they use `jsonify_dto()`. `activate_policy_version` is
update-only with a flat response either way -- `jsonify_dto()` for
consistency with its `create_policy_version` sibling, not because it
needs the workaround.

Table binding: see `blueprints/v1/data_privacy.py`'s module docstring
(same `before_request`-hook rationale, same `bind_privacy_tables` call --
harmless to bind twice, guarded idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import cookie_consent_service as svc
from services.current_user import get_current_user_id, get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.schema import bind_privacy_tables

cookie_consent_bp = Blueprint("v1_cookie_consent", __name__, url_prefix="/api/v1/cookie")

_DEFAULT_VERSION = "1.0.0"


@cookie_consent_bp.before_request
async def _ensure_tables() -> None:
    """Idempotently bind this group's tables -- see module docstring."""
    bind_privacy_tables(current_app.config["dal"])


def _cfg_version() -> str:
    cfg = current_app.config.get("HUB_API_CONFIG")
    version = getattr(cfg, "cookie_consent_version", None)
    return version if version else _DEFAULT_VERSION


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _parse_int(value: str | None, *, default: int, minimum: int, maximum: int | None) -> int:
    """Parse a query-string int, clamped to `[minimum, maximum]`.

    Garbage input falls back to `default` rather than raising.

    Node's `parseInt(..., 10)` silently NaNs on garbage input (then feeds
    NaN into `Math.min`/`Math.max`, an undefined SQL LIMIT); clamping to a
    safe default instead is a strict improvement, not a contract change --
    security.md mandates server-side bounds validation on every input
    regardless of what the upstream Node implementation did with a bad one.
    """
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names are wire contracts pinned to `frontend/src/
# contexts/CookieConsentContext.jsx` (`toApiPayload`/`fromApiConsent`), same
# rationale as `blueprints/v1/auth.py`'s DTO section. The policy/audit DTOs
# further down are deliberately snake_case: Node returns raw DB rows for
# those endpoints (`result.rows[...]`), not a hand-built camelCase object.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ConsentPreferencesDTO:
    """Cookie category + CCPA opt-out preferences."""

    necessary: bool = True
    functional: bool = False
    analytics: bool = False
    marketing: bool = False
    doNotSell: bool = False


@dataclass(slots=True, frozen=True)
class ConsentDataDTO:
    """One consent record -- see module docstring, shared across 4 endpoints."""

    consentId: str | None
    userId: int | None
    preferences: ConsentPreferencesDTO
    version: str
    consentedAt: str | None = None
    expiresAt: str | None = None
    updatedAt: str | None = None
    requiresUpdate: bool = False
    gpcApplied: bool = False


@dataclass(slots=True, frozen=True)
class ConsentResponse:
    """Response DTO for `GET /api/v1/cookie`."""

    success: bool
    data: ConsentDataDTO


@dataclass(slots=True, frozen=True)
class ConsentMessageResponse:
    """Response DTO for save/update/revoke -- `{success, message, data}`."""

    success: bool
    message: str
    data: ConsentDataDTO


@dataclass(slots=True, frozen=True)
class SaveConsentPreferencesDTO:
    """Preferences as submitted to `POST /api/v1/cookie`.

    `doNotSell` is tri-state: `None` means the caller did not say, so a
    stored CCPA opt-out is kept; `True`/`False` is an explicit choice
    (`False` is a deliberate opt-back-in). Defaulting it to `False` would
    make an omitted field indistinguishable from an opt-back-in and
    silently revert the opt-out.
    """

    necessary: bool = True
    functional: bool = False
    analytics: bool = False
    marketing: bool = False
    doNotSell: bool | None = None


@dataclass(slots=True, frozen=True)
class SaveConsentRequest:
    """Request DTO for `POST /api/v1/cookie`."""

    preferences: SaveConsentPreferencesDTO = field(default_factory=SaveConsentPreferencesDTO)
    consentMethod: str = "banner"


@dataclass(slots=True, frozen=True)
class UpdatePreferencesRequest:
    """Request DTO for `PATCH /api/v1/cookie/preferences`.

    `doNotSell` is accepted for wire compatibility but never written from
    the body: this endpoint merges the four cookie categories over the
    stored preferences (`services/cookie_consent_service.py`'s
    `update_preferences()`), so the stored opt-out is preserved, and only a
    `Sec-GPC` signal can force it on.
    """

    preferences: ConsentPreferencesDTO = field(default_factory=ConsentPreferencesDTO)


def _preferences_dto(preferences: dict[str, Any]) -> ConsentPreferencesDTO:
    return ConsentPreferencesDTO(
        necessary=True,
        functional=bool(preferences.get("functional")),
        analytics=bool(preferences.get("analytics")),
        marketing=bool(preferences.get("marketing")),
        doNotSell=bool(preferences.get("doNotSell")),
    )


def _consent_data_dto(record: svc.ConsentRecord, *, gpc_applied: bool = False) -> ConsentDataDTO:
    return ConsentDataDTO(
        consentId=record.consent_id,
        userId=record.user_id,
        preferences=_preferences_dto(record.preferences),
        version=record.version,
        consentedAt=record.consented_at,
        expiresAt=record.expires_at,
        updatedAt=record.updated_at,
        requiresUpdate=record.requires_update,
        gpcApplied=gpc_applied,
    )


def _default_consent_response(version: str) -> ConsentResponse:
    preferences, applied = svc.apply_gpc(request, svc.default_preferences())
    return ConsentResponse(
        success=True,
        data=ConsentDataDTO(
            consentId=None,
            userId=None,
            preferences=_preferences_dto(preferences),
            version=version,
            gpcApplied=applied,
        ),
    )


@cookie_consent_bp.route("", methods=["GET"])
@validate_response(ConsentResponse)
async def get_consent() -> ConsentResponse:
    """Get current user/session consent status."""
    async_dal, dal = _dal()
    user_id = get_optional_current_user_id(request)
    consent_id = request.cookies.get(svc.CONSENT_COOKIE_NAME)
    version = _cfg_version()

    if user_id is None and not consent_id:
        return _default_consent_response(version)

    record = await svc.get_or_create_consent(
        async_dal, dal, user_id=user_id, consent_id=consent_id, current_version=version
    )
    if record is None:
        return _default_consent_response(version)

    # A stored record predates this request, so a GPC signal sent now
    # still has to be reflected in what the client is told (see
    # `apply_gpc()`'s docstring) -- matches `getConsent()`'s own comment.
    preferences, applied = svc.apply_gpc(request, record.preferences)
    return ConsentResponse(
        success=True,
        data=_consent_data_dto(
            svc.ConsentRecord(
                consent_id=record.consent_id,
                user_id=record.user_id,
                preferences=preferences,
                version=record.version,
                consented_at=record.consented_at,
                expires_at=record.expires_at,
                updated_at=record.updated_at,
                requires_update=record.requires_update,
            ),
            gpc_applied=applied,
        ),
    )


@cookie_consent_bp.route("", methods=["POST"])
@validate_request(SaveConsentRequest)
async def save_consent(data: SaveConsentRequest) -> tuple[Any, int]:
    """Save or update consent preferences."""
    async_dal, dal = _dal()
    user_id = get_optional_current_user_id(request)
    requested: dict[str, Any] = {
        "necessary": True,
        "functional": bool(data.preferences.functional),
        "analytics": bool(data.preferences.analytics),
        "marketing": bool(data.preferences.marketing),
    }
    # Only an explicit `doNotSell` is written -- an omitted one must keep
    # the stored opt-out (see `SaveConsentPreferencesDTO`).
    if data.preferences.doNotSell is not None:
        requested["doNotSell"] = data.preferences.doNotSell
    preferences, gpc_applied = svc.apply_gpc(request, requested)
    consent_id = request.cookies.get(svc.CONSENT_COOKIE_NAME)
    version = _cfg_version()

    record = await svc.save_consent(
        async_dal,
        dal,
        user_id=user_id,
        consent_id=consent_id,
        preferences=preferences,
        consent_method=data.consentMethod,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
        version=version,
    )

    response, status = jsonify_dto(
        ConsentMessageResponse(
            success=True,
            message="Cookie preferences saved successfully",
            data=_consent_data_dto(record, gpc_applied=gpc_applied),
        )
    )
    # Matches Node's `res.cookie('waddlebot_consent_id', ...)` -- `secure`
    # unconditional (not `NODE_ENV==='production'`-gated) per security.md's
    # blanket TLS-everywhere requirement; `httponly=False` faithfully
    # ported (the value is an opaque grouping UUID, not a session token).
    response.set_cookie(
        svc.CONSENT_COOKIE_NAME,
        record.consent_id or "",
        max_age=svc.CONSENT_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=False,
        samesite="Lax",
    )
    return response, status


@dataclass(slots=True, frozen=True)
class CookiePolicyDTO:
    """A `cookie_policy_versions` row (full, including `content`)."""

    id: int
    version: str
    content: str
    changes_summary: str | None
    is_active: bool
    effective_date: str | None
    created_at: str | None


@dataclass(slots=True, frozen=True)
class CookiePolicyResponse:
    """Response DTO for `GET /api/v1/cookie/policy`."""

    success: bool
    data: CookiePolicyDTO


@dataclass(slots=True, frozen=True)
class CookiePolicyHistoryItemDTO:
    """A `cookie_policy_versions` row without `content`.

    Matches Node's history query column list.
    """

    id: int
    version: str
    changes_summary: str | None
    is_active: bool
    effective_date: str | None
    created_at: str | None


@dataclass(slots=True, frozen=True)
class CookiePolicyHistoryDataDTO:
    """Paginated policy history payload."""

    versions: list[CookiePolicyHistoryItemDTO]
    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class CookiePolicyHistoryResponse:
    """Response DTO for `GET /api/v1/cookie/policy/history`."""

    success: bool
    data: CookiePolicyHistoryDataDTO


def _policy_dto(policy: svc.PolicyRecord) -> CookiePolicyDTO:
    return CookiePolicyDTO(
        id=policy.id,
        version=policy.version,
        content=policy.content or "",
        changes_summary=policy.changes_summary,
        is_active=policy.is_active,
        effective_date=policy.effective_date,
        created_at=policy.created_at,
    )


def _policy_history_item_dto(policy: svc.PolicyRecord) -> CookiePolicyHistoryItemDTO:
    return CookiePolicyHistoryItemDTO(
        id=policy.id,
        version=policy.version,
        changes_summary=policy.changes_summary,
        is_active=policy.is_active,
        effective_date=policy.effective_date,
        created_at=policy.created_at,
    )


@cookie_consent_bp.route("/policy", methods=["GET"])
@validate_response(CookiePolicyResponse)
async def get_current_policy() -> CookiePolicyResponse | tuple[dict[str, object], int]:
    """Get current active cookie policy."""
    async_dal, dal = _dal()
    policy = await svc.get_current_policy(async_dal, dal)
    if policy is None:
        return cast(
            tuple[dict[str, object], int],
            error_response("No active cookie policy found", 404, "NOT_FOUND"),
        )
    return CookiePolicyResponse(success=True, data=_policy_dto(policy))


@cookie_consent_bp.route("/policy/history", methods=["GET"])
@validate_response(CookiePolicyHistoryResponse)
async def get_policy_history() -> CookiePolicyHistoryResponse:
    """Get all policy versions, paginated."""
    async_dal, dal = _dal()
    limit = _parse_int(request.args.get("limit"), default=10, minimum=1, maximum=50)
    offset = _parse_int(request.args.get("offset"), default=0, minimum=0, maximum=None)
    versions, total = await svc.get_policy_history(async_dal, dal, limit=limit, offset=offset)
    return CookiePolicyHistoryResponse(
        success=True,
        data=CookiePolicyHistoryDataDTO(
            versions=[_policy_history_item_dto(v) for v in versions],
            total=total,
            limit=limit,
            offset=offset,
        ),
    )


@cookie_consent_bp.route("/preferences", methods=["PATCH"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(UpdatePreferencesRequest)
async def update_preferences(data: UpdatePreferencesRequest) -> tuple[Any, int]:
    """Update specific consent categories for the authenticated user.

    The four categories are merged over the stored preferences (a stored
    `doNotSell` opt-out is preserved); a `Sec-GPC: 1` header additionally
    forces `doNotSell` on and `marketing` off, same as `POST /api/v1/cookie`.
    """
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        requested: dict[str, Any] = {
            "necessary": True,
            "functional": bool(data.preferences.functional),
            "analytics": bool(data.preferences.analytics),
            "marketing": bool(data.preferences.marketing),
        }
        preferences, gpc_applied = svc.apply_gpc(request, requested)
        record = await svc.update_preferences(
            async_dal, dal, user_id=user_id, preferences=preferences
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        ConsentMessageResponse(
            success=True,
            message="Preferences updated successfully",
            data=_consent_data_dto(record, gpc_applied=gpc_applied),
        )
    )


@cookie_consent_bp.route("", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def revoke_consent() -> tuple[Any, int]:
    """Revoke all non-essential cookies for the authenticated user."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        record = await svc.revoke_consent(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        ConsentMessageResponse(
            success=True,
            message="All non-essential cookies revoked",
            data=_consent_data_dto(record),
        )
    )


@dataclass(slots=True, frozen=True)
class CookieAuditLogItemDTO:
    """One `cookie_audit_log` row."""

    id: int
    action: str
    category: str | None
    previous_value: bool | None
    new_value: bool | None
    consent_version: str | None
    created_at: str | None


@dataclass(slots=True, frozen=True)
class CookieAuditLogDataDTO:
    """Paginated audit log payload."""

    logs: list[CookieAuditLogItemDTO]
    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class CookieAuditLogResponse:
    """Response DTO for `GET /api/v1/cookie/audit`."""

    success: bool
    data: CookieAuditLogDataDTO


def _audit_entry_dto(entry: svc.AuditLogEntry) -> CookieAuditLogItemDTO:
    return CookieAuditLogItemDTO(
        id=entry.id,
        action=entry.action,
        category=entry.category,
        previous_value=entry.previous_value,
        new_value=entry.new_value,
        consent_version=entry.consent_version,
        created_at=entry.created_at,
    )


@cookie_consent_bp.route("/audit", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(CookieAuditLogResponse)
async def get_audit_log() -> CookieAuditLogResponse | tuple[dict[str, object], int]:
    """Get the authenticated user's own consent audit trail, paginated."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
    except ApiError as exc:
        return _err(exc)
    limit = _parse_int(request.args.get("limit"), default=50, minimum=1, maximum=100)
    offset = _parse_int(request.args.get("offset"), default=0, minimum=0, maximum=None)
    entries, total = await svc.get_audit_log(
        async_dal, dal, user_id=user_id, limit=limit, offset=offset
    )
    return CookieAuditLogResponse(
        success=True,
        data=CookieAuditLogDataDTO(
            logs=[_audit_entry_dto(e) for e in entries], total=total, limit=limit, offset=offset
        ),
    )


@dataclass(slots=True, frozen=True)
class CreatePolicyVersionRequest:
    """Request DTO for `POST /api/v1/cookie/policy` (super admin only)."""

    version: str
    content: str
    changesSummary: str | None = None


@dataclass(slots=True, frozen=True)
class CreatedPolicyDTO:
    """Fields Node's `INSERT ... RETURNING` actually returns."""

    id: int
    version: str
    is_active: bool
    effective_date: str | None
    created_at: str | None


@dataclass(slots=True, frozen=True)
class CreatePolicyVersionResponse:
    """Response DTO for `POST /api/v1/cookie/policy`."""

    success: bool
    message: str
    data: CreatedPolicyDTO


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for `{success, message}`-only endpoints."""

    success: bool
    message: str


@cookie_consent_bp.route("/policy", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("settings:write")  # type: ignore[untyped-decorator]
@validate_request(CreatePolicyVersionRequest)
async def create_policy_version(data: CreatePolicyVersionRequest) -> tuple[Any, int]:
    """Create a new cookie policy version and activate it (super admin only)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
    except ApiError as exc:
        return _err(exc)
    created = await svc.create_policy_version(
        async_dal,
        dal,
        version=data.version,
        content=data.content,
        changes_summary=data.changesSummary,
        created_by=user_id,
    )
    return jsonify_dto(
        CreatePolicyVersionResponse(
            success=True,
            message="Policy version created and activated",
            data=CreatedPolicyDTO(
                id=created.id,
                version=created.version,
                is_active=created.is_active,
                effective_date=created.effective_date,
                created_at=created.created_at,
            ),
        ),
        201,
    )


@cookie_consent_bp.route("/policy/<version>/activate", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("settings:write")  # type: ignore[untyped-decorator]
async def activate_policy_version(version: str) -> tuple[Any, int]:
    """Activate an existing policy version (super admin only)."""
    async_dal, dal = _dal()
    try:
        get_current_user_id(request)  # Node reads req.user?.id for audit logging only
        await svc.activate_policy_version(async_dal, dal, version=version)
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(MessageResponse(success=True, message="Policy version activated"))


BLUEPRINTS: list[Blueprint] = [cookie_consent_bp]
