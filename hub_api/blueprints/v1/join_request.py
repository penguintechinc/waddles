"""v1 `join request` group -- ported from `joinRequestController.js`.

Mounted at `/api/v1` with full paths matching the routes actually live on
Node's Express app: `routes/joinRequests.js`'s router is mounted TWICE in
`routes/index.js` (`router.use('/', joinRequestRoutes)` AND
`router.use('/admin', joinRequestRoutes)`), which -- given Express mount
order (more specific prefixes like `/community`, `/admin` for OTHER
routers are registered earlier and win first) -- makes the root mount live
only at bare `/api/v1/<communityId>/join-requests[...]`, not under any
`/community` prefix. This port keeps the semantically-correct half of that
accidental double-mount: member (self-service) routes at the root mount,
admin routes under `/admin`, matching the ONE route shape each action is
actually reachable at with its real auth requirement (`requireAuth` vs
`requireCommunityAdmin`) attached, rather than reproducing an Express
routing quirk that added no behavioral value in Node either.

NOTE: `admin/hub_module/frontend/src/services/api.js`'s own `joinRequestApi`
object calls unprefixed paths (`/community/${communityId}/join-requests`,
`/admin/${communityId}/join-requests`) that don't match ANY live Node
route (missing the `/api/v1` prefix the whole router is mounted under, and
-- for the member actions -- a `/community` segment `joinRequests.js`
never registers). That is a pre-existing frontend bug, not something this
backend port should silently work around by inventing routes to match it;
this group publishes the real, working `/api/v1`-prefixed contract per
the CONTRACT instruction for this PR.

SECURITY: list/approve/reject are NOT gated by `flask_core.authz.
require_scope` -- see `services/community_authz.py`'s own docstring for
why a flat JWT scope claim is unsafe for a `communityId` path param (the
IDOR class this module's own security review flagged in the Node source).
`services.join_request_service` resolves the caller's admin role from a
live `community_members` DB row keyed on `(community_id_from_url,
user_id_from_JWT)` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import join_request_service as svc
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit

join_request_bp = Blueprint("v1_join_request", __name__, url_prefix="/api/v1")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class SubmitJoinRequestBody:
    """Request DTO for `POST .../join-requests`."""

    message: str | None = None


@dataclass(slots=True, frozen=True)
class SubmittedJoinRequestDTO:
    """Response sub-DTO -- snake_case, matches Node's raw `RETURNING` shape verbatim."""

    id: int
    status: str
    created_at: str


@dataclass(slots=True, frozen=True)
class SubmitJoinRequestResponse:
    """Response DTO for `POST .../join-requests`."""

    success: bool
    request: SubmittedJoinRequestDTO


@dataclass(slots=True, frozen=True)
class MyJoinRequestDTO:
    """Response sub-DTO -- snake_case, matches Node's raw SELECT shape verbatim."""

    id: int
    status: str
    message: str | None
    created_at: str
    reviewed_at: str | None


@dataclass(slots=True, frozen=True)
class MyJoinRequestResponse:
    """Response DTO for `GET .../join-requests/mine`."""

    success: bool
    request: MyJoinRequestDTO | None


@dataclass(slots=True, frozen=True)
class JoinRequestListItemDTO:
    """Response sub-DTO -- mixed casing preserved byte-for-byte (see module docstring)."""

    id: int
    status: str
    message: str | None
    created_at: str
    reviewed_at: str | None
    username: str
    email: str
    avatarUrl: str | None


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination metadata, matches Node's `listRequests` shape."""

    total: int
    page: int
    limit: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class ListJoinRequestsResponse:
    """Response DTO for `GET /api/v1/admin/<communityId>/join-requests`."""

    success: bool
    requests: list[JoinRequestListItemDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class SimpleSuccessResponse:
    """Bare `{success: true}` response -- approve/reject."""

    success: bool


@join_request_bp.route("/<int:community_id>/join-requests", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(SubmitJoinRequestBody)
# NOT @validate_response -- submit_request() awaits insert_async()/
# update_async() then returns a nested-dataclass response, hitting the
# crash documented in services/dto_response.py. jsonify_dto() is the
# equivalent-safety workaround.
async def submit_request(community_id: int, data: SubmitJoinRequestBody) -> tuple[Any, int]:
    """Submit a join request (self-service, own resource)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        submitted = await svc.submit_request(
            async_dal, dal, community_id=community_id, user_id=user_id, message=data.message
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        SubmitJoinRequestResponse(
            success=True,
            request=SubmittedJoinRequestDTO(
                id=submitted.id, status=submitted.status, created_at=submitted.created_at
            ),
        ),
        status=201,
    )


@join_request_bp.route("/<int:community_id>/join-requests/mine", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MyJoinRequestResponse)
async def get_my_request(community_id: int) -> MyJoinRequestResponse:
    """Get the caller's own join request status (self-service, own resource)."""
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    found = await svc.get_my_request(async_dal, dal, community_id=community_id, user_id=user_id)
    if found is None:
        return MyJoinRequestResponse(success=True, request=None)
    return MyJoinRequestResponse(
        success=True,
        request=MyJoinRequestDTO(
            id=found.id,
            status=found.status,
            message=found.message,
            created_at=found.created_at,
            reviewed_at=found.reviewed_at,
        ),
    )


@join_request_bp.route("/admin/<int:community_id>/join-requests", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(ListJoinRequestsResponse)
async def list_requests(
    community_id: int,
) -> ListJoinRequestsResponse | tuple[dict[str, object], int]:
    """List join requests for a community (admin, `community:manage_members|_channels`)."""
    async_dal, dal = _dal()
    status = request.args.get("status", "pending")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    limit = parse_limit(request.args.get("limit"), default=25)

    try:
        admin_user_id = get_current_user_id(request)
        items, total = await svc.list_requests(
            async_dal,
            dal,
            community_id=community_id,
            admin_user_id=admin_user_id,
            status=status,
            page=page,
            limit=limit,
        )
    except ApiError as exc:
        return _err(exc)

    page = max(1, page)
    limit = min(50, max(1, limit))
    return ListJoinRequestsResponse(
        success=True,
        requests=[
            JoinRequestListItemDTO(
                id=i.id,
                status=i.status,
                message=i.message,
                created_at=i.created_at,
                reviewed_at=i.reviewed_at,
                username=i.username,
                email=i.email,
                avatarUrl=i.avatarUrl,
            )
            for i in items
        ],
        pagination=PaginationDTO(
            total=total,
            page=page,
            limit=limit,
            totalPages=(total + limit - 1) // limit if limit else 0,
        ),
    )


@join_request_bp.route(
    "/admin/<int:community_id>/join-requests/<int:request_id>/approve", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@validate_response(SimpleSuccessResponse)
async def approve_request(
    community_id: int, request_id: int
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Approve a pending join request (admin, `community:manage_members|_channels`)."""
    async_dal, dal = _dal()
    try:
        reviewer_id = get_current_user_id(request)
        await svc.approve_request(
            async_dal,
            dal,
            community_id=community_id,
            request_id=request_id,
            reviewer_id=reviewer_id,
        )
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


@join_request_bp.route(
    "/admin/<int:community_id>/join-requests/<int:request_id>/reject", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@validate_response(SimpleSuccessResponse)
async def reject_request(
    community_id: int, request_id: int
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Reject a pending join request (admin, `community:manage_members|_channels`)."""
    async_dal, dal = _dal()
    try:
        reviewer_id = get_current_user_id(request)
        await svc.reject_request(
            async_dal,
            dal,
            community_id=community_id,
            request_id=request_id,
            reviewer_id=reviewer_id,
        )
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


BLUEPRINTS: list[Blueprint] = [join_request_bp]
