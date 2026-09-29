"""v1 `bundle_activation` group -- COMMUNITY tier of the App Bundle 3-tier lifecycle.

Mount: `/api/v1/apps/community/<community_id>/activation`. Activate/
deactivate require community-ADMIN membership (`services.community_authz.
authorize_community(..., admin=True)`, the DB-backed per-community check
-- never a flat JWT scope, see that module's own IDOR-closing rationale);
GET requires only active community MEMBERSHIP (`admin=False`) --
`community_id` is otherwise an IDOR vector across tenants, closed by
`authorize_community`'s own tenant-ownership re-check.

See `services/bundle_approval_service.py`'s module docstring for the full
3-tier split; this tier's service layer (`activate_for_community()`/
`deactivate_for_community()`/`list_community_activations()`) lives in
that same module (it owns the transactional AUTO-BIND plumbing shared
with the GLOBAL tier's own writes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_approval_service as svc
from services.community_authz import authorize_community
from services.current_user import get_current_user_id
from services.errors import ApiError

bundle_activation_bp = Blueprint(
    "v1_bundle_activation", __name__, url_prefix="/api/v1/apps/community"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _dal() -> tuple[Any, Any]:
    """`(async_dal, dal)` -- the pydal handles `authorize_community()` needs for membership."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class ActivateRequest:
    """Request DTO for `POST /community/<community_id>/activation`."""

    appId: str


@dataclass(slots=True, frozen=True)
class ActivationDTO:
    """Response DTO: one `app_active_versions` row for a COMMUNITY-tier activation."""

    appId: str
    communityId: int
    tenantId: int
    versionId: int
    activatedAt: str | None


@dataclass(slots=True, frozen=True)
class ActivationListResponse:
    """Response DTO for `GET /community/<community_id>/activation`."""

    success: bool
    bundles: list[ActivationDTO]


def _activation_dto(row: Any) -> ActivationDTO:
    return ActivationDTO(
        appId=row.app_id,
        communityId=row.community_id,
        tenantId=row.tenant_id,
        versionId=row.version_id,
        activatedAt=_iso(row.activated_at),
    )


@bundle_activation_bp.route("/<int:community_id>/activation", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(ActivationListResponse)
async def list_activation(
    community_id: int,
) -> ActivationListResponse | tuple[dict[str, object], int]:
    """List apps activated for this community -- requires active community membership."""
    install_dal = _install_dal()
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
    except ApiError as exc:
        return _err(exc)
    rows = await svc.list_community_activations(install_dal, community_id=community_id)
    return ActivationListResponse(success=True, bundles=[_activation_dto(r) for r in rows])


@bundle_activation_bp.route("/<int:community_id>/activation", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(ActivateRequest)
@validate_response(MessageResponse, 201)
async def activate(
    data: ActivateRequest, community_id: int
) -> tuple[MessageResponse | dict[str, object], int]:
    """Activate an app for this community. Requires community-admin membership.

    409 if the app is not available in this community's tenant
    marketplace (TENANT tier gate) or conflicts with `routes_to`
    validation; 404 if `community_id` does not belong to the caller's
    tenant.
    """
    install_dal = _install_dal()
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        await svc.activate_for_community(
            install_dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            app_id=data.appId,
            activated_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{data.appId} activated"), 201


@bundle_activation_bp.route("/<int:community_id>/activation/<app_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def deactivate(
    community_id: int, app_id: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate an app for this community (hot-unload). Requires community-admin membership."""
    install_dal = _install_dal()
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        await svc.deactivate_for_community(
            install_dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            app_id=app_id,
            deactivated_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{app_id} deactivated")


BLUEPRINTS: list[Blueprint] = [bundle_activation_bp]
