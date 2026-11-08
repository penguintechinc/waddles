"""v1 internal data-plane active-context -- the invoking user's active community for a source.

* ``GET /api/v1/internal/dataplane/active-context?platform=&source_id=&user=``
* ``PUT /api/v1/internal/dataplane/active-context`` -- body ``{platform, sourceId, user, ...}``

Machine JWT only: scope ``dataplane:active-context`` from the operator-plane ``system`` tenant,
granted in the chart to svc-ingest's SPIFFE identity alone. ``user`` is the tokenized user UUID
(hub-api never sees a platform user id); ``community`` is a community ``public_id``. The answer
carries the NUMERIC ``communityId`` (the key the data plane routes on) beside the ``public_id`` and
display data, through an explicit DTO. Every response is ``no-store``: it is per-user and mutable.
Contract with svc-ingest: ``core/svc_ingest/src/active_context.rs`` (``ContextView``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from flask_core.api_utils import error_response
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_request, validate_response

from services import active_context as ctx
from services.machine_auth import machine_authenticated

logger = logging.getLogger(__name__)

dataplane_active_context_bp = Blueprint(
    "v1_dataplane_active_context_internal",
    __name__,
    url_prefix="/api/v1/internal/dataplane",
)

#: Machine-JWT scope the data plane's service identity must hold (and no other service should).
SCOPE_DATAPLANE_ACTIVE_CONTEXT = "dataplane:active-context"


@dataclass(slots=True, frozen=True)
class CommunityDTO:
    """One switchable community (``position`` 0 = first-registered default)."""

    publicId: str
    displayName: str
    position: int


@dataclass(slots=True, frozen=True)
class ActiveContextDTO:
    """The user's active context; ``communityId`` is the numeric routing key."""

    communityPublicId: str
    communityId: int
    communityName: str
    explicit: bool
    communities: list[CommunityDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SetContextRequest:
    """Body of the PUT form."""

    platform: str
    sourceId: str
    user: str
    community: str


_machine_authenticated = machine_authenticated(
    SCOPE_DATAPLANE_ACTIVE_CONTEXT,
    unconfigured_event="dataplane_active_context.service_auth_unconfigured",
    rejected_event="dataplane_active_context.service_auth_rejected",
    wrong_tenant_event="dataplane_active_context.service_auth_wrong_tenant",
)


@dataplane_active_context_bp.after_request
async def _no_store(response: Response) -> Response:
    """Per-user mutable data is never cacheable."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _dto(found: ctx.ActiveContext) -> ActiveContextDTO:
    return ActiveContextDTO(
        communityPublicId=found.active.public_id,
        communityId=found.active.community_id,
        communityName=found.active.display_name,
        explicit=found.explicit,
        communities=[
            CommunityDTO(c.public_id, c.display_name, c.position) for c in found.communities
        ],
    )


def _refusal(exc: ctx.ActiveContextError) -> tuple[dict[str, object], int]:
    """Map a refusal to the JSON error envelope (fixed, application-authored text)."""
    if isinstance(exc, ctx.InvalidContextRequestError):
        status, code, message = 400, "BAD_REQUEST", "platform, source_id, user or community invalid"
    elif isinstance(exc, ctx.AmbiguousSourceError):
        status, code, message = 409, "SCOPE_AMBIGUOUS", "source maps to more than one tenant"
    elif isinstance(exc, ctx.NotSwitchableError):
        status, code, message = 404, "COMMUNITY_NOT_SWITCHABLE", "community is not switchable here"
    else:
        status, code, message = 404, "SCOPE_NOT_FOUND", "no community is linked to this source"
    return error_response(message, status, code)  # type: ignore[no-any-return]


@dataplane_active_context_bp.route("/active-context", methods=["GET"])
@_machine_authenticated
@validate_response(ActiveContextDTO)
async def get_active_context() -> Any:
    """``GET`` -- the user's active community for the source (default = first-registered)."""
    try:
        found = await ctx.get_context(
            current_app.config["async_dal"],
            request.args.get("platform", ""),
            request.args.get("source_id", ""),
            request.args.get("user", ""),
        )
    except ctx.ActiveContextError as exc:
        logger.debug("active_context.refused reason=%s", type(exc).__name__)
        return _refusal(exc)
    return _dto(found)


@dataplane_active_context_bp.route("/active-context", methods=["PUT"])
@_machine_authenticated
@validate_request(SetContextRequest)
@validate_response(ActiveContextDTO)
async def put_active_context(data: SetContextRequest) -> Any:
    """``PUT`` -- switch the user's active community (by ``public_id``) within the source's set."""
    try:
        found = await ctx.set_context(
            current_app.config["async_dal"], data.platform, data.sourceId, data.user, data.community
        )
    except ctx.ActiveContextError as exc:
        logger.debug("active_context.refused reason=%s", type(exc).__name__)
        return _refusal(exc)
    return _dto(found)


BLUEPRINTS = [dataplane_active_context_bp]
