"""v1 `community.raffle` group -- port of Node's `raffleCustomizationController.js` (M6 Community).

Per-event-type custom sound + message-template config, mounted under
`/api/v1/admin` matching Node's `routes/raffleCustomization.js`. File
upload uses Quart's native multipart form parsing (`request.files`) in
place of Node's `multer` memory-storage middleware.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from dataclasses import asdict

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services.community_common import community_in_tenant
from services.community_raffle import (
    MAX_FILE_SIZE_BYTES,
    VALID_EVENT_TYPES,
    delete_customization,
    get_customizations,
    store_sound,
    upsert_customization,
)

raffle_bp = Blueprint("v1_community_raffle", __name__, url_prefix="/api/v1/admin")

#: Two-gate Feature flag -- `libs/community_module/features.py`'s
#: `community.raffles` Feature contract, Professional tier.
FEATURE_COMMUNITY_RAFFLES = "waddles.community.raffles"

_INVALID_EVENT_TYPE_MESSAGE = (
    f"Invalid event type. Must be one of: {', '.join(sorted(VALID_EVENT_TYPES))}"
)


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


@raffle_bp.route("/<int:community_id>/raffle-customization", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.raffle:read")  # type: ignore[misc]
async def list_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/raffle-customization`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_RAFFLES, tenant=ctx.tenant_slug):
        return {
            "success": False,
            "error": {"message": "Community raffles require a Professional plan or higher"},
        }, 402
    if not _tenant_ok(community_id):
        return {"success": False, "error": {"message": "Community not found"}}, 404
    customizations = get_customizations(current_app.config["dal"], community_id)
    return {
        "success": True,
        "customizations": {k: asdict(v) for k, v in customizations.items()},
    }, 200


@raffle_bp.route("/<int:community_id>/raffle-customization/<event_type>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.raffle:write")  # type: ignore[misc]
async def upsert_route(community_id: int, event_type: str) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/raffle-customization/<eventType>`."""
    if not _tenant_ok(community_id):
        return {"success": False, "error": {"message": "Community not found"}}, 404
    payload = await request.get_json(force=True, silent=True) or {}
    dto = upsert_customization(
        current_app.config["dal"],
        community_id,
        event_type,
        payload.get("message_template"),
        payload.get("is_active"),
    )
    if dto is None:
        return {"success": False, "error": {"message": _INVALID_EVENT_TYPE_MESSAGE}}, 400
    return {"success": True, "customization": asdict(dto)}, 200


@raffle_bp.route("/<int:community_id>/raffle-customization/<event_type>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.raffle:write")  # type: ignore[misc]
async def delete_route(community_id: int, event_type: str) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/raffle-customization/<eventType>`."""
    if not _tenant_ok(community_id):
        return {"success": False, "error": {"message": "Community not found"}}, 404
    result = delete_customization(current_app.config["dal"], community_id, event_type)
    if result is None:
        return {"success": False, "error": {"message": _INVALID_EVENT_TYPE_MESSAGE}}, 400
    return {"success": True}, 200


@raffle_bp.route("/<int:community_id>/raffle-customization/<event_type>/upload", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.raffle:write")  # type: ignore[misc]
async def upload_route(community_id: int, event_type: str) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/raffle-customization/<eventType>/upload` -- max 2MB, mp3/ogg/wav."""
    if not _tenant_ok(community_id):
        return {"success": False, "error": {"message": "Community not found"}}, 404

    files = await request.files
    upload = files.get("sound")
    if upload is None:
        return {"success": False, "error": {"message": "No file uploaded"}}, 400

    data = upload.read()
    if len(data) > MAX_FILE_SIZE_BYTES:
        return {"success": False, "error": {"message": "File exceeds 2MB limit"}}, 400

    dto, err = store_sound(
        current_app.config["dal"], community_id, event_type, upload.filename or "", len(data), data
    )
    if err:
        return {"success": False, "error": {"message": err}}, 400
    assert dto is not None  # nosec B101 -- store_sound: dto is None only when err is set
    return {"success": True, "customization": asdict(dto)}, 200


BLUEPRINTS: list[Blueprint] = [raffle_bp]
