"""v1 `bundle_tenant_availability` group -- TENANT tier of the App Bundle 3-tier lifecycle.

Mount: `/api/v1/apps/tenant/<tenant_slug>/availability`. Gated
`require_scope("tenant:admin")` + `require_matching_tenant` for
enable/disable (same pattern `blueprints/v1/tenant.py`/the older
`blueprints/v1/marketplace_lifecycle.py` already use for their own
tenant-tier routes); GET needs only `tenant_middleware` (any authenticated
member of the tenant -- a community admin deciding what to activate must
be able to see what is available without also holding `tenant:admin`,
same rationale `marketplace_lifecycle.py`'s own module docstring gives).

See `services/bundle_approval_service.py`'s module docstring for the full
3-tier split; this tier's service layer is `services/
tenant_app_availability_service.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import tenant_app_availability_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

bundle_tenant_availability_bp = Blueprint(
    "v1_bundle_tenant_availability", __name__, url_prefix="/api/v1/apps/tenant"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _tenant_id(tenant_slug: str) -> int:
    """Validate the URL's `tenant_slug` against the caller's own `TenantContext`, return its id."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this on the success path
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class SetAvailableRequest:
    """Request DTO for `POST /tenant/<tenant_slug>/availability`."""

    appId: str
    pinnedVersionId: int | None = None


@dataclass(slots=True, frozen=True)
class AvailabilityDTO:
    """Response DTO: one `bundle_tenant_availability` row."""

    appId: str
    tenantId: int
    available: bool
    pinnedVersionId: int | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class AvailabilityListResponse:
    """Response DTO for `GET /tenant/<tenant_slug>/availability`."""

    success: bool
    bundles: list[AvailabilityDTO]


def _availability_dto(row: Any) -> AvailabilityDTO:
    return AvailabilityDTO(
        appId=row.app_id,
        tenantId=row.tenant_id,
        available=bool(row.available),
        pinnedVersionId=row.pinned_version_id,
        updatedAt=_iso(row.updated_at),
    )


@bundle_tenant_availability_bp.route("/<tenant_slug>/availability", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(AvailabilityListResponse)
async def list_availability(
    tenant_slug: str,
) -> AvailabilityListResponse | tuple[dict[str, object], int]:
    """List every `bundle_tenant_availability` row for this tenant (enabled and disabled alike)."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
    except ApiError as exc:
        return _err(exc)
    rows = await svc.list_availability(install_dal, tenant_id=tenant_id)
    return AvailabilityListResponse(success=True, bundles=[_availability_dto(r) for r in rows])


@bundle_tenant_availability_bp.route("/<tenant_slug>/availability", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(SetAvailableRequest)
@validate_response(MessageResponse, 201)
async def enable_availability(
    data: SetAvailableRequest, tenant_slug: str
) -> tuple[MessageResponse | dict[str, object], int]:
    """Enable a globally-installed app in this tenant's marketplace. 409 if not installed."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.set_available(
            install_dal,
            tenant_id=tenant_id,
            app_id=data.appId,
            updated_by=get_current_user_id(request),
            pinned_version_id=data.pinnedVersionId,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{data.appId} made available"), 201


@bundle_tenant_availability_bp.route("/<tenant_slug>/availability/<app_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def disable_availability(
    tenant_slug: str, app_id: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Disable an app in this tenant's marketplace. Cascades: deactivates it in every community."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.unset_available(
            install_dal, tenant_id=tenant_id, app_id=app_id, updated_by=get_current_user_id(request)
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{app_id} made unavailable")


BLUEPRINTS: list[Blueprint] = [bundle_tenant_availability_bp]
