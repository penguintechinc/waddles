"""v1 member-facing community routes: self-create and recent activity.

`POST /api/v1/communities` (flag `waddles.community-self-create`, default OFF -> 403
`feature_disabled`) creates a community in the CALLER's tenant (token, never the body). The
caller must hold the tenant scope `communities:create` (403 before any cap logic). The body
carries no `platformServerId`: a guild is bound only via the verified pairing/bot-install flow.
`GET /api/v1/communities/<cuuid>/activity` is the member read (active, non-banned members and
tenant/global admins; foreign-tenant community -> 404). Logic lives in
`services.community_self_service`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, cast

from flask_core.api_utils import error_response
from flask_core.auth import SCOPE_COMMUNITIES_CREATE
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from opentelemetry import metrics
from pydantic import Field
from quart import Blueprint, current_app, request
from quart_schema import RequestSchemaValidationError, validate_request, validate_response

from services import community_self_service as svc
from services.community_public_id import lookup_public_id
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.route_guards import community_member_guard
from services.superadmin_service import VALID_COMMUNITY_TYPES

logger = logging.getLogger(__name__)
_created = metrics.get_meter(__name__).create_counter(
    "community_self_create_total", description="Self-created communities"
)

community_self_bp = Blueprint("v1_community_self", __name__, url_prefix="/api/v1/communities")


@dataclass(slots=True, frozen=True)
class SelfCreateRequest:
    """Body for self-create; tenant is never accepted from the body."""

    name: Annotated[str, Field(min_length=1, max_length=100)]
    displayName: Annotated[str | None, Field(max_length=100)] = None  # noqa: N815
    description: Annotated[str | None, Field(max_length=2000)] = None
    platform: Annotated[str, Field(min_length=1, max_length=50)] = "discord"
    communityType: str | None = None  # noqa: N815


@dataclass(slots=True, frozen=True)
class SelfCreatedCommunityDTO:
    """Created community: public UUID + names only."""

    id: str
    name: str
    displayName: str | None  # noqa: N815


@dataclass(slots=True, frozen=True)
class SelfCreateResponse:
    """201 envelope."""

    success: bool
    community: SelfCreatedCommunityDTO


@dataclass(slots=True, frozen=True)
class ActivityResponse:
    """Activity feed envelope."""

    success: bool
    items: list[svc.ActivityItem]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core error envelope."""
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


@community_self_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_COMMUNITIES_CREATE)  # type: ignore[misc]
@validate_request(SelfCreateRequest)
async def create_community(data: SelfCreateRequest) -> tuple[Any, int]:
    """Self-create a community in the caller's tenant (scope + flag gated, free-tier capped)."""
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(ApiError("Tenant context required", 403, "FORBIDDEN"))
    if not await feature_enabled(
        svc.FEATURE_COMMUNITY_SELF_CREATE, tenant=ctx.tenant_slug, default=False
    ):
        return cast(tuple[Any, int], error_response("Feature disabled", 403, "feature_disabled"))
    if data.communityType is not None and data.communityType not in VALID_COMMUNITY_TYPES:
        return _err(ApiError("Invalid community type", 400, "VALIDATION_ERROR"))
    try:
        row = await svc.create_own_community(
            current_app.config["async_dal"],
            current_app.config["dal"],
            caller_id=get_current_user_id(request),
            tenant_slug=ctx.tenant_slug,
            tenant_id=ctx.tenant_id,
            name=data.name,
            display_name=data.displayName,
            description=data.description,
            platform=data.platform,
            community_type=data.communityType,
        )
    except ApiError as exc:
        logger.info("community_self_create_rejected", extra={"status": exc.status_code})
        return _err(exc)
    _created.add(1)
    logger.info("community_self_created", extra={"tenant": ctx.tenant_slug})
    return jsonify_dto(
        SelfCreateResponse(
            success=True,
            community=SelfCreatedCommunityDTO(
                id=await lookup_public_id(int(row.id)),
                name=row.name,
                displayName=row.display_name,
            ),
        ),
        201,
    )


@community_self_bp.route("/<cuuid:community_id>/activity", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@community_member_guard
@validate_response(ActivityResponse)
async def get_activity(community_id: int) -> ActivityResponse:
    """Newest-first member-joined / announcement feed for an active, non-banned member."""
    raw = request.args.get("limit", "20")
    limit = min(50, max(1, int(raw))) if raw.lstrip("-").isdigit() else 20
    items = svc.list_activity(current_app.config["dal"], community_id=community_id, limit=limit)
    return ActivityResponse(success=True, items=items)


@community_self_bp.errorhandler(RequestSchemaValidationError)  # type: ignore[misc]
async def _request_validation_error(exc: RequestSchemaValidationError) -> Any:
    """Return a JSON 400 envelope naming each invalid field."""
    err = getattr(exc, "validation_error", None)
    issues = err.errors() if err is not None else []
    parts = [
        f"{'.'.join(str(p) for p in i.get('loc', ())) or 'body'}: {i.get('msg')}" for i in issues
    ]
    return error_response("Invalid request: " + "; ".join(parts), 400, "VALIDATION_ERROR")


BLUEPRINTS: list[Blueprint] = [community_self_bp]
