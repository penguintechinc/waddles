"""v1 `bundle_approvals` group -- GET permissions, POST approve/deny (spec Sec9.7).

R52 (coordinator ruling): every handler reads
`current_app.config["install_dal"]` -- `app_install_approvals`/
`app_version_uploads`/`app_versions` are this milestone's own new
tables. Global tier (`platform:admin`) -- the admin approving/denying
an install-time consent screen is a platform-wide action (spec Sec9.7.2).
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


@dataclass(slots=True, frozen=True)
class PermissionSummaryResponse:
    """Response DTO for `GET .../permissions`."""

    success: bool
    summary: dict[str, Any]
    permissionHash: str


@dataclass(slots=True, frozen=True)
class ApproveRequest:
    """Request DTO for `POST .../approve`."""

    communityId: int | None = None
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
async def post_approve(
    data: ApproveRequest, app_id: str, version: str
) -> tuple[dict[str, object], int]:
    """Approve a version. A headless caller supplies `permissionHash`; a mismatch fails closed."""
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    caller_id = get_current_user_id(request)
    try:
        row = await svc.approve_version(
            install_dal,
            app_id=app_id,
            version=version,
            tenant_id=ctx.tenant_id,
            community_id=data.communityId,
            approved_by=caller_id,
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
    return {"success": True, "permissionHash": row.permission_hash}, 200


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


BLUEPRINTS: list[Blueprint] = [bundle_approvals_bp]
