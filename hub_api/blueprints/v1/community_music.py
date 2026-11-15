"""v1 `community.music` group -- member-facing music queue page read.

`GET /api/v1/communities/<community_id>/music` returns now-playing, the
upcoming queue (<=50), the last 5 played tracks and whether the optional AI
DJ is running, read from the music-requests service bundle's tables on the
READ REPLICA (`services.community_music_service`). Auth: `tenant_middleware`
first, then `authorize_community(admin=False)` -- tenant ownership of the
community is re-validated on every call (IDOR hardening) and the caller must
be an active member (403 otherwise, same as the sibling community blueprints).
Behind the `waddles.music.queue-page` flag (default OFF -> 404). The response
never carries requester identity -- see the DTOs in the service module.
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import community_music_service as svc
from services.community_authz import authorize_community
from services.errors import ApiError, not_found

#: PostHog flag gating the whole page; default OFF until validated.
FEATURE_MUSIC_QUEUE_PAGE = "waddles.music.queue-page"

community_music_bp = Blueprint("v1_community_music", __name__, url_prefix="/api/v1/communities")


def _dals() -> tuple[Any, Any, Any, Any]:
    """Return `(async_dal, primary_dal, read_dal, install_dal)`; read_dal falls back to primary."""
    async_dal = current_app.config["async_dal"]
    read_dal = async_dal.read_dal if async_dal.read_dal is not None else async_dal.dal
    return async_dal, async_dal.dal, read_dal, current_app.config["install_dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@community_music_bp.route("/<int:community_id>/music", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(svc.MusicPageDTO)
async def get_music(community_id: int) -> svc.MusicPageDTO | tuple[dict[str, object], int]:
    """Return the community's music page (members only, replica read, flag-gated)."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always runs first
    if not await feature_enabled(
        FEATURE_MUSIC_QUEUE_PAGE, tenant=ctx.tenant_slug, community=community_id, default=False
    ):
        return _err(not_found("Not found"))

    async_dal, dal, read_dal, install_dal = _dals()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
        return await svc.get_music_page(
            async_dal,
            read_dal,
            install_dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
        )
    except ApiError as exc:
        return _err(exc)


BLUEPRINTS: list[Blueprint] = [community_music_bp]
