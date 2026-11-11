"""`POST /api/v1/communities/<communityId>/join` -- direct join of an open community."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services import community_join_service as svc
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError

community_join_bp = Blueprint("v1_community_join", __name__, url_prefix="/api/v1/communities")
BLUEPRINTS = [community_join_bp]


@dataclass(slots=True, frozen=True)
class JoinCommunityResponse:
    """Response DTO: exact fields `success`, `community_id`, `joined` (False = already a member)."""

    success: bool
    community_id: int
    joined: bool


@community_join_bp.route("/<int:community_id>/join", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def join_community(community_id: int) -> tuple[Any, int]:
    """Join an open community (idempotent); tenant comes from the token only."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        user_id = get_current_user_id(request)
        result = await svc.join_open_community(
            current_app.config["async_dal"],
            current_app.config["dal"],
            community_id=community_id,
            user_id=user_id,
            tenant_id=ctx.tenant_id,
        )
    except ApiError as exc:
        return cast(tuple[Any, int], error_response(exc.message, exc.status_code, exc.code))
    return jsonify_dto(
        JoinCommunityResponse(success=True, community_id=result.community_id, joined=result.joined),
        status=201 if result.joined else 200,
    )
