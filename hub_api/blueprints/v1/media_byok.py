"""v1 media (Gemini) BYOK -- community-admin key API + internal resolver/tier endpoints.

Spec: `docs/superpowers/specs/2026-11-06-harness-media-generation.md` (PR3). Two blueprints:

* `media_byok_admin_bp` (`/api/v1/admin/<community_id>/media/byok-keys`) -- set/rotate, status,
  delete. Community-admin only (`require_community_admin`, tenant-bound), Professional-or-higher
  required to SET (a downgraded community can still delete). The key is never returned: responses
  carry `key_last4` only. Audited by the HTTP audit hook (`audit_events._INLINE_AUTHZ_GROUPS`).
* `media_byok_internal_bp` (`/api/v1/internal/media`) -- machine-JWT/SVID only, for the Rust media
  host. `byok/resolve` returns the decrypted key (the ONLY place a key leaves hub-api);
  `effective-tier` returns `max(tenant, community)`. The caller identity is allow-listed, every
  resolve is rate-limited per (caller, community) and audited (who/when/outcome, never the key).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, Response, current_app, g, request
from quart_schema import validate_request, validate_response

from services import media_byok_service as media
from services.community_access import require_community_admin
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.machine_auth import machine_authenticated

logger = logging.getLogger(__name__)

media_byok_admin_bp = Blueprint("v1_media_byok_admin", __name__, url_prefix="/api/v1/admin")
media_byok_internal_bp = Blueprint(
    "v1_media_byok_internal", __name__, url_prefix="/api/v1/internal/media"
)

#: Machine-JWT scopes (granted in the chart to svc-process alone -- the media stage).
SCOPE_MEDIA_BYOK_RESOLVE: Final = "media:byok:resolve"
SCOPE_MEDIA_TIER_RESOLVE: Final = "media:tier:resolve"

#: Service names (last SPIFFE path segment) allowed to call the internal routes; defence in depth on
#: top of the chart's scope grant. `MEDIA_BYOK_ALLOWED_SERVICES` (comma list) overrides.
DEFAULT_ALLOWED_SERVICES: Final = ("svc-process",)
#: Per (caller, community) resolver budget.
RESOLVE_LIMIT: Final = 30
RESOLVE_WINDOW_SECONDS: Final = 60


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class MediaByokKeyDTO:
    """Masked key record -- never carries the real key."""

    provider: str
    key_last4: str
    is_active: bool
    created_at: str | None
    updated_at: str | None
    rotated_at: str | None


@dataclass(slots=True, frozen=True)
class MediaByokStatusResponse:
    """`GET` response: `configured` is true iff an ACTIVE key is on file."""

    success: bool
    configured: bool
    keys: list[MediaByokKeyDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SetMediaByokRequest:
    """`PUT` body -- set-or-rotate one provider's key."""

    provider: str
    api_key: str


@dataclass(slots=True, frozen=True)
class MediaMessageResponse:
    """Generic `{success, message}` response."""

    success: bool
    message: str


def _key_dto(info: Any) -> MediaByokKeyDTO:
    return MediaByokKeyDTO(
        provider=info.provider,
        key_last4=info.key_last4,
        is_active=info.is_active,
        created_at=_iso(info.created_at),
        updated_at=_iso(info.updated_at),
        rotated_at=_iso(info.rotated_at),
    )


async def _require_admin(community_id: int) -> tuple[int, int]:
    """Enforce community-admin inside the caller's tenant; returns `(tenant_id, user_id)`."""
    ctx = get_tenant_context(request)
    if ctx is None or ctx.tenant_id is None:
        raise ApiError("Tenant context not resolved", 403, "FORBIDDEN")
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    await require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return int(ctx.tenant_id), user_id


@media_byok_admin_bp.route("/<int:community_id>/media/byok-keys", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MediaByokStatusResponse)
async def get_media_byok_status(
    community_id: int,
) -> MediaByokStatusResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<community_id>/media/byok-keys` -- masked status, never the key."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _require_admin(community_id)
        keys = await media.media_key_status(
            async_dal, dal, tenant_id=tenant_id, community_id=community_id
        )
    except ApiError as exc:
        return _err(exc)
    return MediaByokStatusResponse(
        success=True,
        configured=any(k.is_active for k in keys),
        keys=[_key_dto(k) for k in keys],
    )


@media_byok_admin_bp.route("/<int:community_id>/media/byok-keys", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(SetMediaByokRequest)
@validate_response(MediaByokKeyDTO)
async def set_media_byok_key(
    data: SetMediaByokRequest, community_id: int
) -> MediaByokKeyDTO | tuple[dict[str, object], int]:
    """`PUT .../media/byok-keys` -- Professional+ required; validated against Gemini first.

    Never logs or echoes `data.api_key`; the response is the masked record.
    """
    async_dal, dal = _dal()
    try:
        tenant_id, user_id = await _require_admin(community_id)
        info = await media.set_media_key(
            async_dal,
            dal,
            tenant_id=tenant_id,
            community_id=community_id,
            provider=data.provider,
            api_key=data.api_key,
            user_id=user_id,
        )
    except ApiError as exc:
        return _err(exc)
    return _key_dto(info)


@media_byok_admin_bp.route("/<int:community_id>/media/byok-keys/<provider>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MediaMessageResponse)
async def delete_media_byok_key(
    community_id: int, provider: str
) -> MediaMessageResponse | tuple[dict[str, object], int]:
    """`DELETE .../media/byok-keys/<provider>` -- deactivates; allowed at any tier."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _require_admin(community_id)
        await media.delete_media_key(
            async_dal, dal, tenant_id=tenant_id, community_id=community_id, provider=provider
        )
    except ApiError as exc:
        return _err(exc)
    return MediaMessageResponse(success=True, message=f"{provider} key deactivated")


# ===== Internal (machine identity only) =====


@dataclass(slots=True, frozen=True)
class ResolveRequest:
    """Resolver body. Numeric ids only (data-plane keys never use slugs)."""

    tenant_id: int
    community_id: int
    provider: str


@dataclass(slots=True, frozen=True)
class ResolveResponse:
    """`status` is `ok` (api_key set) | `not_configured` | `tier_required`."""

    status: str
    tier: str
    api_key: str | None = None


@dataclass(slots=True, frozen=True)
class TierRequest:
    """Effective-tier body."""

    tenant_id: int
    community_id: int


@dataclass(slots=True, frozen=True)
class TierResponse:
    """`tier` is `free` | `professional` | `enterprise` (fail-closed to free)."""

    tier: str


_resolve_auth = machine_authenticated(
    SCOPE_MEDIA_BYOK_RESOLVE,
    unconfigured_event="media_byok.service_auth_unconfigured",
    rejected_event="media_byok.service_auth_rejected",
    wrong_tenant_event="media_byok.service_auth_wrong_tenant",
)
_tier_auth = machine_authenticated(
    SCOPE_MEDIA_TIER_RESOLVE,
    unconfigured_event="media_tier.service_auth_unconfigured",
    rejected_event="media_tier.service_auth_rejected",
    wrong_tenant_event="media_tier.service_auth_wrong_tenant",
)


@media_byok_internal_bp.after_request
async def _no_store(response: Response) -> Response:
    """Nothing here may be cached by an intermediary (a body can hold a key)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _allowed_services() -> frozenset[str]:
    raw = os.getenv("MEDIA_BYOK_ALLOWED_SERVICES")
    if raw is None:
        return frozenset(DEFAULT_ALLOWED_SERVICES)
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _caller_service() -> str | None:
    """Service name of the verified caller (last SPIFFE segment), or None if not allow-listed."""
    subject = str(getattr(g, "machine_subject", "") or "")
    name = subject.rsplit("/", 1)[-1] if subject.startswith("spiffe://") else ""
    return name if name in _allowed_services() else None


async def _audit(action: str, caller: str, req: Any, outcome: str, tier: str | None) -> None:
    """Write one `audit_log` row (who/when/outcome). Never carries the key; raises on failure."""
    async_dal, dal = _dal()
    details: dict[str, object] = {
        "caller": caller,
        "tenant_id": req.tenant_id,
        "community_id": req.community_id,
        "outcome": outcome,
    }
    if tier is not None:
        details["tier"] = tier
    if hasattr(req, "provider"):
        details["provider"] = req.provider
    await async_dal.insert_async(
        dal.audit_log,
        user_id=None,
        action=action,
        target_type="media_byok",
        target_id=str(req.community_id),
        details=details,
        created_at=datetime.now(UTC),
    )


def _bad_ids(req: Any) -> bool:
    return any(
        isinstance(v, bool) or not isinstance(v, int) or v < 0
        for v in (req.tenant_id, req.community_id)
    )


@media_byok_internal_bp.route("/byok/resolve", methods=["POST"])
@_resolve_auth
@validate_request(ResolveRequest)
@validate_response(ResolveResponse)
async def resolve_byok(data: ResolveRequest) -> Any:
    """`POST /api/v1/internal/media/byok/resolve` -> `{status, tier, api_key?}`.

    Fail-closed ladder after machine auth: caller allow-list (403) -> rate limit (429, or 503 when
    no limiter is wired) -> input shape (400) -> tenant/community scope (404) -> tier -> key.
    The audit row is written BEFORE a key is returned; if it cannot be written the key is withheld
    (500) -- a resolve that cannot be audited does not happen.
    """
    caller = _caller_service()
    if caller is None:
        logger.warning("media_byok.caller_not_allowlisted")
        return {"error": "forbidden"}, 403
    limiter = current_app.config.get("rate_limiter")
    if limiter is None:
        logger.error("media_byok.rate_limiter_unconfigured")
        return {"error": "rate limiter is not configured"}, 503
    if not await limiter.check_rate_limit(
        f"media_byok_resolve:{caller}:{data.community_id}", RESOLVE_LIMIT, RESOLVE_WINDOW_SECONDS
    ):
        return {"error": "rate limit exceeded"}, 429
    if _bad_ids(data) or data.provider not in media.SUPPORTED_MEDIA_PROVIDERS:
        return error_response("invalid request", 400, "BAD_REQUEST")
    async_dal, dal = _dal()
    try:
        result = await media.resolve_media_key(
            async_dal,
            dal,
            tenant_id=data.tenant_id,
            community_id=data.community_id,
            provider=data.provider,
        )
    except media.CommunityScopeError:
        await _audit("media.byok.resolve", caller, data, "scope_mismatch", None)
        return error_response("community not found", 404, "NOT_FOUND")
    try:
        await _audit("media.byok.resolve", caller, data, result.status, result.tier)
    except Exception:
        logger.error("media_byok.audit_write_failed", exc_info=True)
        return {"error": "audit unavailable"}, 500
    return ResolveResponse(status=result.status, tier=result.tier, api_key=result.api_key)


@media_byok_internal_bp.route("/effective-tier", methods=["POST"])
@_tier_auth
@validate_request(TierRequest)
@validate_response(TierResponse)
async def effective_tier_route(data: TierRequest) -> Any:
    """`POST /api/v1/internal/media/effective-tier` -> `{tier}` = max(tenant, community)."""
    if _caller_service() is None:
        logger.warning("media_tier.caller_not_allowlisted")
        return {"error": "forbidden"}, 403
    if _bad_ids(data):
        return error_response("invalid request", 400, "BAD_REQUEST")
    async_dal, dal = _dal()
    try:
        tier = await media.effective_tier(
            async_dal, dal, tenant_id=data.tenant_id, community_id=data.community_id
        )
    except media.CommunityScopeError:
        return error_response("community not found", 404, "NOT_FOUND")
    return TierResponse(tier=tier)


BLUEPRINTS = [media_byok_admin_bp, media_byok_internal_bp]
