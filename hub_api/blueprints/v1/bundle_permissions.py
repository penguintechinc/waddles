"""v1 `bundle_permissions` group -- the Android-style permission consent flow (3 tiers + revoke).

Mount: `/api/v1/apps` (GLOBAL), `/api/v1/apps/tenant/<tenant_slug>` (TENANT),
`/api/v1/apps/community/<community_id>` (COMMUNITY) -- same URL-prefix
family as `bundle_approvals.py`/`bundle_tenant_availability.py`/
`bundle_activation.py`, one blueprint per tier's own auth mechanism:

- GLOBAL (`platform:admin`): approve the requested permission catalog for
  one `(app_id, version)` -- `services.bundle_permission_service.
  record_permission_requests()`.
- TENANT (`tenant:admin` + `require_matching_tenant`): restrict (exclude)
  a subset of the globally-approved set tenant-wide.
- COMMUNITY (`community_authz.authorize_community(..., admin=True)`):
  grant at activation (mandatory full consent) and revoke a single
  permission without deactivating the whole bundle.

See `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-
gate.md` Sec3 for the full flow this wires up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_instance_policy_service as instance_policy_svc
from services import bundle_permission_service as svc
from services.community_authz import authorize_community
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

bundle_permissions_bp = Blueprint("v1_bundle_permissions", __name__, url_prefix="/api/v1/apps")


async def _reject_instance_denied(install_dal: AsyncDB, permission_ids: frozenset[str]) -> None:
    """403 `instance_denied_permission` if any id's TYPE is instance-denied (spec: instance policy).

    Checked ABOVE the existing 3 tiers -- an instance-wide deny applies to
    every bundle regardless of its own catalog approval/tenant/community
    consent, so this runs before `record_permission_requests`/
    `grant_community_permissions` ever gets a chance to write a row.
    """
    denied = [
        pid
        for pid in sorted(permission_ids)
        if await instance_policy_svc.is_instance_denied(install_dal, permission_id=pid)
    ]
    if denied:
        raise ApiError(f"instance policy denies: {denied}", 403, "instance_denied_permission")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _dal() -> tuple[Any, Any]:
    """`(async_dal, dal)` -- the pydal handles `authorize_community()` needs for membership."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _tenant_id(tenant_slug: str) -> int:
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
class ApprovePermissionsRequest:
    """Request DTO for GLOBAL tier `POST .../permissions/approve` (spec Sec3.1)."""

    approvedPermissions: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class RestrictPermissionsRequest:
    """Request DTO for TENANT tier `PUT .../permissions/restrict` (spec Sec3.2)."""

    restrictedPermissionIds: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class GrantPermissionsRequest:
    """Request DTO for COMMUNITY tier `POST .../permissions/grant` (spec Sec3.3)."""

    version: str
    grantedPermissions: list[str] = field(default_factory=list)
    paramsById: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class GrantVersionResponse:
    """Response DTO carrying the bumped `grant_version` -- security.md Output Validation."""

    success: bool
    grantVersion: int


@dataclass(slots=True, frozen=True)
class PermissionListResponse:
    """Response DTO for every GET permission-id-set listing in this group."""

    success: bool
    permissionIds: list[str]


# ---------------------------------------------------------------------------
# GLOBAL tier
# ---------------------------------------------------------------------------


@bundle_permissions_bp.route("/<app_id>/versions/<version>/permissions/approve", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(ApprovePermissionsRequest)
@validate_response(MessageResponse)
async def approve_permissions(
    data: ApprovePermissionsRequest, app_id: str, version: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """GLOBAL tier: approve the requested permission catalog for one `(app_id, version)`."""
    install_dal = _install_dal()
    try:
        manifest = await svc.manifest_for_version(install_dal, app_id=app_id, version=version)
        await _reject_instance_denied(
            install_dal, frozenset(d.id for d in manifest.permission_declarations)
        )
        await svc.record_permission_requests(
            install_dal,
            app_id=app_id,
            version=version,
            declarations=manifest.permission_declarations,
            approved_by=get_current_user_id(request),
            approved_permissions=frozenset(data.approvedPermissions),
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"permissions approved for {app_id}@{version}")


@bundle_permissions_bp.route("/<app_id>/versions/<version>/permissions/approved", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(PermissionListResponse)
async def list_approved_permissions(app_id: str, version: str) -> PermissionListResponse:
    """The GLOBAL-tier approved ceiling for `(app_id, version)` -- any authenticated tenant."""
    install_dal = _install_dal()
    ids = await svc.get_approved_permission_ids(install_dal, app_id=app_id, version=version)
    return PermissionListResponse(success=True, permissionIds=sorted(ids))


# ---------------------------------------------------------------------------
# TENANT tier
# ---------------------------------------------------------------------------


@bundle_permissions_bp.route(
    "/tenant/<tenant_slug>/<app_id>/versions/<version>/permissions/restrict", methods=["PUT"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(RestrictPermissionsRequest)
@validate_response(MessageResponse)
async def restrict_permissions(
    data: RestrictPermissionsRequest, tenant_slug: str, app_id: str, version: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """TENANT tier: set this tenant's exclusion list for `app_id` (opt-out, never opt-in)."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.restrict_tenant_permissions(
            install_dal,
            tenant_id=tenant_id,
            app_id=app_id,
            version=version,
            restricted_permission_ids=frozenset(data.restrictedPermissionIds),
            restricted_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"tenant restrictions set for {app_id}")


# ---------------------------------------------------------------------------
# COMMUNITY tier
# ---------------------------------------------------------------------------


@bundle_permissions_bp.route(
    "/community/<int:community_id>/<app_id>/permissions/grant", methods=["POST"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(GrantPermissionsRequest)
@validate_response(GrantVersionResponse)
async def grant_permissions(
    data: GrantPermissionsRequest, community_id: int, app_id: str
) -> tuple[GrantVersionResponse | dict[str, object], int]:
    """COMMUNITY tier: the activation-prompt consent -- mandatory full grant, never partial."""
    install_dal = _install_dal()
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        manifest = await svc.manifest_for_version(install_dal, app_id=app_id, version=data.version)
        await _reject_instance_denied(install_dal, frozenset(data.grantedPermissions))
        new_version = await svc.grant_community_permissions(
            install_dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            app_id=app_id,
            version=data.version,
            manifest=manifest,
            granted_permission_ids=frozenset(data.grantedPermissions),
            params_by_id=data.paramsById,
            granted_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return GrantVersionResponse(success=True, grantVersion=new_version), 201


@bundle_permissions_bp.route(
    "/community/<int:community_id>/<app_id>/permissions/<permission_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(GrantVersionResponse)
async def revoke_permission(
    community_id: int, app_id: str, permission_id: str
) -> GrantVersionResponse | tuple[dict[str, object], int]:
    """COMMUNITY tier (spec Sec3.7): revoke one permission without deactivating the whole bundle."""
    install_dal = _install_dal()
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    version = request.args.get("version", "")
    if not version:
        return _err(ApiError("version query param is required", 400, "BAD_REQUEST"))
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        new_version = await svc.deactivate_permission(
            install_dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            app_id=app_id,
            version=version,
            permission_id=permission_id,
            deactivated_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return GrantVersionResponse(success=True, grantVersion=new_version)


@bundle_permissions_bp.route(
    "/community/<int:community_id>/<app_id>/permissions/granted", methods=["GET"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(PermissionListResponse)
async def list_granted_permissions(
    community_id: int, app_id: str
) -> PermissionListResponse | tuple[dict[str, object], int]:
    """The community's current grant set for `app_id` -- requires active community membership."""
    install_dal = _install_dal()
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
    except ApiError as exc:
        return _err(exc)
    ids = await svc.get_community_granted_ids(install_dal, community_id=community_id, app_id=app_id)
    return PermissionListResponse(success=True, permissionIds=sorted(ids))


# ---------------------------------------------------------------------------
# INSTANCE policy tier (above GLOBAL/TENANT/COMMUNITY) -- platform:admin only
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InstancePolicyDTO:
    """One `instance_permission_policies` row, DTO-shaped for the API response."""

    permissionKey: str
    paramScope: str | None
    action: str


@dataclass(slots=True, frozen=True)
class ListInstancePoliciesResponse:
    """Response DTO for `GET .../permissions/instance-policy`."""

    success: bool
    policies: list[InstancePolicyDTO]


@dataclass(slots=True, frozen=True)
class SetInstancePolicyRequest:
    """Request DTO for `PUT .../permissions/instance-policy` (spec: instance policy)."""

    permissionKey: str
    action: str
    paramScope: str | None = None


@dataclass(slots=True, frozen=True)
class SetInstancePolicyResponse:
    """Response DTO carrying how many existing community grants a `deny` cascade revoked."""

    success: bool
    cascadedRevocations: int


@bundle_permissions_bp.route("/permissions/instance-policy", methods=["GET"])
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(ListInstancePoliciesResponse)
async def list_instance_policies() -> ListInstancePoliciesResponse:
    """Every explicit instance-policy row -- global-admin-only, spans every tenant by design."""
    install_dal = _install_dal()
    rows = await instance_policy_svc.list_policies(install_dal)
    return ListInstancePoliciesResponse(
        success=True,
        policies=[
            InstancePolicyDTO(
                permissionKey=r.permission_key, paramScope=r.param_scope, action=r.action
            )
            for r in rows
        ],
    )


@bundle_permissions_bp.route("/permissions/instance-policy", methods=["PUT"])
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(SetInstancePolicyRequest)
@validate_response(SetInstancePolicyResponse)
async def set_instance_policy(
    data: SetInstancePolicyRequest,
) -> SetInstancePolicyResponse | tuple[dict[str, object], int]:
    """Allow/deny a permission TYPE instance-wide -- applies to every bundle, every tenant.

    Enabling a `deny` on a type that was previously `allow`/unset cascades
    revocation of every matching active `community_permission_grants` row
    (spec: instance policy) -- see `bundle_instance_policy_service.
    set_instance_policy` for the one-transaction cascade + audit shape.
    """
    if data.action not in ("allow", "deny"):
        return _err(ApiError("action must be 'allow' or 'deny'", 400, "invalid_action"))
    install_dal = _install_dal()
    cascaded = await instance_policy_svc.set_instance_policy(
        install_dal,
        permission_key=data.permissionKey,
        action=cast(Any, data.action),
        param_scope=data.paramScope,
        set_by=get_current_user_id(request),
    )
    return SetInstancePolicyResponse(success=True, cascadedRevocations=cascaded)


BLUEPRINTS: list[Blueprint] = [bundle_permissions_bp]
