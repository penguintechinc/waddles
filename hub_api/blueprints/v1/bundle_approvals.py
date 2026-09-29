"""v1 `bundle_approvals` group -- GET permissions, GLOBAL tier install/uninstall/list (spec Sec9.7).

R52 (coordinator ruling): every handler reads
`current_app.config["install_dal"]` -- `app_version_uploads`/`app_versions`/
`app_global_installs` are hub-api-owned control-plane tables. GLOBAL tier
(`platform:admin`) -- installing a version into the platform catalog is a
platform-wide action; it no longer activates anything for any tenant or
community (see `services/bundle_approval_service.py`'s own module
docstring for the full 3-tier split -- TENANT tier is
`blueprints/v1/bundle_tenant_availability.py`, COMMUNITY tier is
`blueprints/v1/bundle_activation.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_approval_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError

bundle_approvals_bp = Blueprint("v1_bundle_approvals", __name__, url_prefix="/api/v1/apps")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class PermissionSummaryResponse:
    """Response DTO for `GET .../permissions`."""

    success: bool
    summary: dict[str, Any]
    permissionHash: str


@dataclass(slots=True, frozen=True)
class ApproveRequest:
    """Request DTO for `POST .../approve` (GLOBAL tier install -- no tenant/community anymore)."""

    permissionHash: str | None = None


@dataclass(slots=True, frozen=True)
class DenyRequest:
    """Request DTO for `POST .../deny`."""

    reason: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class ApproveResponse:
    """Response DTO for a successful `POST .../approve` (security.md Output Validation)."""

    success: bool
    permissionHash: str


@dataclass(slots=True, frozen=True)
class GlobalInstallDTO:
    """Response DTO: one `app_global_installs` row."""

    appId: str
    version: str
    installSource: str
    installedAt: str | None
    revokedAt: str | None


@dataclass(slots=True, frozen=True)
class GlobalInstallListResponse:
    """Response DTO for `GET /api/v1/apps/installs`."""

    success: bool
    installs: list[GlobalInstallDTO]


def _install_dto(row: Any) -> GlobalInstallDTO:
    return GlobalInstallDTO(
        appId=row.app_id,
        version=row.version,
        installSource=row.install_source,
        installedAt=_iso(row.installed_at),
        revokedAt=_iso(row.revoked_at),
    )


@bundle_approvals_bp.route("/<app_id>/versions/<version>/permissions", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(PermissionSummaryResponse)
async def get_permissions(
    app_id: str, version: str
) -> PermissionSummaryResponse | tuple[dict[str, object], int]:
    """The consent summary and its hash -- inspected by a headless caller before approving."""
    install_dal = _install_dal()
    try:
        summary, computed_hash = await svc.get_permission_summary(
            install_dal, app_id=app_id, version=version
        )
    except ApiError as exc:
        return _err(exc)
    return PermissionSummaryResponse(success=True, summary=summary, permissionHash=computed_hash)


@bundle_approvals_bp.route("/<app_id>/versions/<version>/approve", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(ApproveRequest)
@validate_response(ApproveResponse)
async def post_approve(
    data: ApproveRequest, app_id: str, version: str
) -> tuple[ApproveResponse | dict[str, object], int]:
    """GLOBAL tier: install a version into the platform catalog. No tenant/community activation."""
    install_dal = _install_dal()
    caller_id = get_current_user_id(request)
    try:
        row = await svc.install_version_globally(
            install_dal,
            app_id=app_id,
            version=version,
            installed_by=caller_id,
            expected_permission_hash=data.permissionHash,
        )
    except ApiError as exc:
        if exc.code in ("permission_hash_mismatch",):
            summary, current_hash = await svc.get_permission_summary(
                install_dal, app_id=app_id, version=version
            )
            return (
                {
                    "success": False,
                    "error": {"code": exc.code, "message": exc.message},
                    "summary": summary,
                    "permissionHash": current_hash,
                },
                409,
            )
        return _err(exc)
    return ApproveResponse(success=True, permissionHash=row.permission_hash), 200


@bundle_approvals_bp.route("/<app_id>/versions/<version>/deny", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(DenyRequest)
@validate_response(MessageResponse)
async def post_deny(
    data: DenyRequest, app_id: str, version: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Deny a version -- moves it to REJECTED with `reason`."""
    install_dal = _install_dal()
    try:
        await svc.deny_version(install_dal, app_id=app_id, version=version, reason=data.reason)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(
        success=True, message=f"version {version} of {app_id} denied: {data.reason}"
    )


@bundle_approvals_bp.route("/<app_id>/uninstall", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def post_uninstall(app_id: str) -> MessageResponse | tuple[dict[str, object], int]:
    """GLOBAL tier: revoke `app_id`'s platform-catalog install.

    Cascades: hidden in every tenant's marketplace + deactivated in every
    community that had it running (`services.bundle_approval_service.
    uninstall_globally()`'s own cascade).
    """
    install_dal = _install_dal()
    try:
        await svc.uninstall_globally(
            install_dal, app_id=app_id, revoked_by=get_current_user_id(request)
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{app_id} uninstalled")


@bundle_approvals_bp.route("/installs", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(GlobalInstallListResponse)
async def list_installs() -> GlobalInstallListResponse:
    """List the platform catalog's current installs -- any authenticated tenant member."""
    install_dal = _install_dal()
    rows = await svc.list_global_installs(install_dal)
    return GlobalInstallListResponse(success=True, installs=[_install_dto(r) for r in rows])


BLUEPRINTS: list[Blueprint] = [bundle_approvals_bp]
