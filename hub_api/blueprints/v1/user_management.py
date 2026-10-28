"""v1 `superadmin users` group -- ported from `userManagementController.js`.

Mounted at `/api/v1/superadmin/users*` (matches `routes/superadmin.js`).
Gated by `require_scope("users:admin")` -- see `services/
user_management_service.py`'s module docstring for why that scope is the
OIDC-native equivalent of Node's `requireSuperAdmin` boolean check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import user_management_service as svc
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit

user_management_bp = Blueprint(
    "v1_superadmin_users", __name__, url_prefix="/api/v1/superadmin/users"
)

logger = logging.getLogger(__name__)


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


@dataclass(slots=True, frozen=True)
class UserDTO:
    """User DTO."""

    id: int
    email: str | None
    username: str | None
    avatarUrl: str | None = None
    isActive: bool = True
    isSuperAdmin: bool = False
    isVendor: bool = False
    emailVerified: bool | None = None
    isAnalyticsConsumer: bool | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class ListUsersResponse:
    """Response DTO for list users endpoints."""

    success: bool
    users: list[UserDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class UserResponse:
    """Response DTO for user endpoints."""

    success: bool
    user: UserDTO


@dataclass(slots=True, frozen=True)
class CreateUserRequest:
    """Request DTO for create user endpoints."""

    email: str
    password: str


@dataclass(slots=True, frozen=True)
class UpdateUserRequest:
    """Request DTO for update user endpoints."""

    email: str | None = None
    isActive: bool | None = None


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for message endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class GrantRequest:
    """Request DTO for grant endpoints."""

    grant: bool


@dataclass(slots=True, frozen=True)
class VerifiedRequest:
    """Request DTO for verified endpoints."""

    verified: bool


@dataclass(slots=True, frozen=True)
class AnalyticsConsumerRequest:
    """Request DTO for analytics consumer endpoints."""

    enabled: bool


@dataclass(slots=True, frozen=True)
class AnalyticsConsumerResponse:
    """Response DTO for analytics consumer endpoints."""

    success: bool
    user: UserDTO


@dataclass(slots=True, frozen=True)
class DeletionRequestDTO:
    """Deletion request DTO."""

    requestedAt: str | None
    completedAt: str | None
    status: str | None


@dataclass(slots=True, frozen=True)
class DeletionRequestResponse:
    """Response DTO for deletion request endpoints."""

    success: bool
    deletion_request: DeletionRequestDTO | None


@dataclass(slots=True, frozen=True)
class PasswordResetResponse:
    """Response DTO for password reset endpoints."""

    success: bool
    message: str
    resetToken: str
    resetExpires: str


@dataclass(slots=True, frozen=True)
class SessionDTO:
    """One login session -- identifiers and timestamps only, never a token or digest."""

    id: int
    createdAt: str | None
    expiresAt: str | None
    isActive: bool
    revokedAt: str | None


@dataclass(slots=True, frozen=True)
class SessionsResponse:
    """Response DTO for the list-sessions endpoint."""

    success: bool
    sessions: list[SessionDTO]


@dataclass(slots=True, frozen=True)
class RevokeSessionsResponse:
    """Response DTO for the revoke endpoints.

    `accessTokensRevokedImmediately` is `False` when the shared revocation store could not be
    written: refresh is cut off regardless (database), but already-issued access tokens survive
    until their own expiry (<= 15 minutes) on other replicas -- the operator should retry.
    """

    success: bool
    sessionsRevoked: int
    accessTokensRevokedImmediately: bool


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _row_dto(row: Any) -> UserDTO:
    return UserDTO(
        id=row.id,
        email=row.email,
        username=row.username,
        avatarUrl=getattr(row, "avatar_url", None),
        isActive=bool(row.is_active),
        isSuperAdmin=bool(row.is_super_admin),
        isVendor=bool(row.is_vendor),
        emailVerified=bool(getattr(row, "email_verified", False))
        if hasattr(row, "email_verified")
        else None,
        isAnalyticsConsumer=bool(getattr(row, "is_analytics_consumer", False))
        if hasattr(row, "is_analytics_consumer")
        else None,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(getattr(row, "updated_at", None)),
    )


@user_management_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(ListUsersResponse)
async def list_users() -> ListUsersResponse:
    """List users."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    role = request.args.get("role")
    is_active_param = request.args.get("isActive")
    is_active = is_active_param == "true" if is_active_param is not None else None

    rows, total, total_pages = await svc.list_users(
        async_dal, dal, page=page, limit=limit, search=search, role=role, is_active=is_active
    )
    return ListUsersResponse(
        success=True,
        users=[_row_dto(r) for r in rows],
        pagination=PaginationDTO(
            page=page, limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@user_management_bp.route("/<int:user_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(UserResponse)
async def get_user(user_id: int) -> UserResponse | tuple[dict[str, object], int]:
    """Get user."""
    async_dal, dal = _dal()
    try:
        row = await svc.get_user(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return UserResponse(success=True, user=_row_dto(row))


@user_management_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(CreateUserRequest)
# NOT @validate_response -- create_user() inserts into hub_users, hitting
# the crash documented in services/dto_response.py. jsonify_dto() below is
# the equivalent-safety workaround (see blueprints/v1/auth.py for the
# original, fully-isolated repro of this same crash class).
async def create_user(data: CreateUserRequest) -> tuple[Any, int]:
    """Create user."""
    async_dal, dal = _dal()
    try:
        row = await svc.create_user(async_dal, dal, email=data.email, password=data.password)
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(UserResponse(success=True, user=_row_dto(row)), 201)


@user_management_bp.route("/<int:user_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(UpdateUserRequest)
# NOT @validate_response -- same crash class (update_async + nested-dataclass
# response); see services/dto_response.py.
async def update_user(data: UpdateUserRequest, user_id: int) -> tuple[Any, int]:
    """Update user."""
    async_dal, dal = _dal()
    try:
        row = await svc.update_user(
            async_dal, dal, user_id=user_id, email=data.email, is_active=data.isActive
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(UserResponse(success=True, user=_row_dto(row)))


@user_management_bp.route("/<int:user_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_user(user_id: int) -> MessageResponse | tuple[dict[str, object], int]:
    """Delete user."""
    async_dal, dal = _dal()
    try:
        caller_id = get_current_user_id(request)
        await svc.delete_user(async_dal, dal, user_id=user_id, caller_id=caller_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="User deleted")


@user_management_bp.route("/<int:user_id>/super-admin-role", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(GrantRequest)
@validate_response(MessageResponse)
async def assign_super_admin_role(
    data: GrantRequest, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Assign super admin role."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(ApiError("Tenant context required", 403, "FORBIDDEN"))
    try:
        caller_id = get_current_user_id(request)
        changed = await svc.assign_super_admin_role(
            async_dal,
            dal,
            user_id=user_id,
            grant=data.grant,
            caller_id=caller_id,
            tenant_slug=ctx.tenant_slug,
        )
    except ApiError as exc:
        logger.info("api_error_rejected", extra={"code": exc.code, "status": exc.status_code})
        return _err(exc)
    verb = "has" if data.grant else "does not have"
    message = (
        f"Super admin role {'granted' if data.grant else 'revoked'}"
        if changed
        else f"User already {verb} super admin role"
    )
    return MessageResponse(success=True, message=message)


@user_management_bp.route("/<int:user_id>/vendor-role", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(GrantRequest)
@validate_response(MessageResponse)
async def assign_vendor_role(
    data: GrantRequest, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Assign vendor role."""
    async_dal, dal = _dal()
    try:
        changed = await svc.assign_vendor_role(async_dal, dal, user_id=user_id, grant=data.grant)
    except ApiError as exc:
        return _err(exc)
    verb = "has" if data.grant else "does not have"
    message = (
        f"Vendor role {'granted' if data.grant else 'revoked'}"
        if changed
        else f"User already {verb} vendor role"
    )
    return MessageResponse(success=True, message=message)


@user_management_bp.route("/<int:user_id>/verify-email", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(VerifiedRequest)
@validate_response(MessageResponse)
async def set_email_verification(
    data: VerifiedRequest, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Set email verification."""
    async_dal, dal = _dal()
    try:
        changed = await svc.set_email_verification(
            async_dal, dal, user_id=user_id, verified=data.verified
        )
    except ApiError as exc:
        return _err(exc)
    state = "verified" if data.verified else "unverified"
    message = f"Email {state}" if changed else f"User email is already {state}"
    return MessageResponse(success=True, message=message)


@user_management_bp.route("/<int:user_id>/password-reset", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(PasswordResetResponse)
async def generate_password_reset(
    user_id: int,
) -> PasswordResetResponse | tuple[dict[str, object], int]:
    """Generate password reset."""
    async_dal, dal = _dal()
    try:
        reset_token, reset_expires = await svc.generate_password_reset(
            async_dal, dal, user_id=user_id
        )
    except ApiError as exc:
        return _err(exc)
    return PasswordResetResponse(
        success=True,
        message="Password reset token generated",
        resetToken=reset_token,
        resetExpires=reset_expires.isoformat(),
    )


@user_management_bp.route("/<int:user_id>/sessions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(SessionsResponse)
async def list_user_sessions(user_id: int) -> SessionsResponse | tuple[dict[str, object], int]:
    """List a user's login sessions (newest first) so one can be revoked by id."""
    async_dal, dal = _dal()
    try:
        sessions = await svc.list_user_sessions(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        logger.info(
            "%s refused: status=%s code=%s", "list_user_sessions", exc.status_code, exc.code
        )
        return _err(exc)
    return SessionsResponse(
        success=True,
        sessions=[
            SessionDTO(
                id=s.session_id,
                createdAt=_iso(s.created_at),
                expiresAt=_iso(s.expires_at),
                isActive=s.is_active,
                revokedAt=_iso(s.revoked_at),
            )
            for s in sessions
        ],
    )


@user_management_bp.route("/<int:user_id>/revoke-sessions", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(RevokeSessionsResponse)
async def revoke_user_sessions(
    user_id: int,
) -> RevokeSessionsResponse | tuple[dict[str, object], int]:
    """Revoke every session of a user: refresh stops now, every access token issued so far dies."""
    async_dal, dal = _dal()
    try:
        outcome = await svc.revoke_user_sessions(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        logger.info(
            "%s refused: status=%s code=%s", "revoke_user_sessions", exc.status_code, exc.code
        )
        return _err(exc)
    return RevokeSessionsResponse(
        success=True,
        sessionsRevoked=outcome.sessions_revoked,
        accessTokensRevokedImmediately=outcome.access_tokens_recorded,
    )


@user_management_bp.route("/<int:user_id>/sessions/<int:session_id>/revoke", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(RevokeSessionsResponse)
async def revoke_user_session(
    user_id: int, session_id: int
) -> RevokeSessionsResponse | tuple[dict[str, object], int]:
    """Revoke one login session of a user (404 if the session is not theirs)."""
    async_dal, dal = _dal()
    try:
        outcome = await svc.revoke_user_session(
            async_dal, dal, user_id=user_id, session_id=session_id
        )
    except ApiError as exc:
        logger.info(
            "%s refused: status=%s code=%s", "revoke_user_session", exc.status_code, exc.code
        )
        return _err(exc)
    return RevokeSessionsResponse(
        success=True,
        sessionsRevoked=outcome.sessions_revoked,
        accessTokensRevokedImmediately=outcome.access_tokens_recorded,
    )


@user_management_bp.route("/<int:user_id>/analytics-consumer-role", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(AnalyticsConsumerRequest)
# NOT @validate_response -- same crash class (update_async + nested-dataclass
# response); see services/dto_response.py.
async def assign_analytics_consumer_role(
    data: AnalyticsConsumerRequest, user_id: int
) -> tuple[Any, int]:
    """Assign analytics consumer role."""
    async_dal, dal = _dal()
    try:
        row = await svc.assign_analytics_consumer_role(
            async_dal, dal, user_id=user_id, enabled=data.enabled
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(AnalyticsConsumerResponse(success=True, user=_row_dto(row)))


@user_management_bp.route("/<int:user_id>/deletion-request", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(DeletionRequestResponse)
async def get_user_deletion_request(user_id: int) -> DeletionRequestResponse:
    """Get user deletion request."""
    async_dal, dal = _dal()
    row = await svc.get_user_deletion_request(async_dal, dal, user_id=user_id)
    if row is None:
        return DeletionRequestResponse(success=True, deletion_request=None)
    requested_at, completed_at, status = row
    return DeletionRequestResponse(
        success=True,
        deletion_request=DeletionRequestDTO(
            requestedAt=_iso(requested_at), completedAt=_iso(completed_at), status=status
        ),
    )


BLUEPRINTS: list[Blueprint] = [user_management_bp]
