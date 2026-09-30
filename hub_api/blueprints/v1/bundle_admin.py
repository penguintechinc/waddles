"""v1 `bundle_admin` group -- `GET /api/v1/admin/bundle-versions` (global-admin approval queue).

Minimal, read-only cross-app companion to `blueprints/v1/bundle_versions.py`
(per-`app_id` list/detail) and `blueprints/v1/bundle_approvals.py`
(per-version approve/deny). Neither existing group can answer "every
version currently awaiting approval, across every vendor/first-party
`app_id`" -- exactly what the hub-webui global-admin queue
(`SuperAdminBundleApprovals.jsx`) needs to render without querying every
`app_id` it happens to know about. `platform:admin` only, same tier as
`bundle_approvals.py`'s approve/deny endpoints this page's actions call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_version_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.pagination import parse_limit

bundle_admin_bp = Blueprint("v1_bundle_admin", __name__, url_prefix="/api/v1/admin")


def _install_dal() -> Any:
    return current_app.config["install_dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return cast(str | None, value)


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO -- matches `blueprints/v1/platform.py`'s own shape."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class BundleVersionSummaryDTO:
    """One `app_version_uploads` row, for the approval-queue list view."""

    versionId: int
    appId: str
    version: str
    status: str
    requestedBy: int | None
    createdAt: str | None
    rejectReason: str | None


@dataclass(slots=True, frozen=True)
class ListBundleVersionsResponse:
    """Response DTO for `GET /api/v1/admin/bundle-versions`."""

    success: bool
    versions: list[BundleVersionSummaryDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class AbandonUploadRequest:
    """Request DTO for `POST /api/v1/admin/bundle-versions/<app_id>/<version>/abandon`."""

    reason: str


@dataclass(slots=True, frozen=True)
class AbandonUploadResponse:
    """Response DTO for a successful abandon (security.md Output Validation)."""

    success: bool
    appId: str
    version: str
    status: str


@bundle_admin_bp.route("/bundle-versions", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(ListBundleVersionsResponse)
async def list_bundle_versions() -> ListBundleVersionsResponse | tuple[dict[str, object], int]:
    """Cross-app bundle-version upload rows, newest first (default `?status=pending`).

    `status` aliases: `pending` -> `PUBLISHED` (staged, awaiting a
    global-admin approve/deny decision -- see
    `services/bundle_version_service.py`'s `_STATUS_ALIASES` docstring);
    any other literal `app_version_uploads.status` value (e.g.
    `REJECTED`) is accepted as-is.
    """
    install_dal = _install_dal()
    status = request.args.get("status", "pending")
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)

    try:
        rows, total = await svc.list_versions_by_status(
            install_dal, status=status, page=page, limit=limit
        )
    except ApiError as exc:
        return _err(exc)

    resolved_limit = min(100, max(1, limit))
    total_pages = (total + resolved_limit - 1) // resolved_limit if total else 0
    return ListBundleVersionsResponse(
        success=True,
        versions=[
            BundleVersionSummaryDTO(
                versionId=row.id,
                appId=row.app_id,
                version=row.version,
                status=row.status,
                requestedBy=row.requested_by,
                createdAt=_iso(row.created_at),
                rejectReason=row.reject_reason,
            )
            for row in rows
        ],
        pagination=PaginationDTO(
            page=max(1, page), limit=resolved_limit, total=total, totalPages=total_pages
        ),
    )


@bundle_admin_bp.route("/bundle-versions/<app_id>/<version>/abandon", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(AbandonUploadRequest)
@validate_response(AbandonUploadResponse)
async def abandon_bundle_version(
    data: AbandonUploadRequest, app_id: str, version: str
) -> AbandonUploadResponse | tuple[dict[str, object], int]:
    """Global-admin-only escape hatch for a stuck, non-terminal `app_version_uploads` row.

    The `waddles.core.*` namespace never needs this -- `hub_api/cli/seed_core_bundles.py`
    self-heals a stalled row automatically once its lease expires
    (`services.bundle_version_service.abandon_stalled_upload()`, same function this
    route calls). A vendor/tenant upload has no such automated reclaim (deliberately --
    only a human, `platform:admin`-scoped decision abandons someone else's in-flight
    upload), so this route is that explicit, audited action: it works for ANY `app_id`,
    core or vendor, moving the row straight to `ABANDONED` regardless of lease age.
    """
    install_dal = _install_dal()
    caller_id = get_current_user_id(request)
    try:
        row = await svc.abandon_stalled_upload(
            install_dal,
            app_id=app_id,
            version=version,
            reason=data.reason,
            actor=f"admin:{caller_id}",
            actor_id=caller_id,
        )
    except ApiError as exc:
        return _err(exc)
    return AbandonUploadResponse(success=True, appId=app_id, version=version, status=row.status)


BLUEPRINTS: list[Blueprint] = [bundle_admin_bp]
