"""v1 `community.forms` group -- port of Node's `formsController.js` (M6 Community).

`formsController.js` is itself a pure reverse proxy to `core-engagement`
(no local DB access) -- see `services/community_engagement_proxy.py`'s
docstring for why `@validate_response` is intentionally skipped here.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services import community_engagement_proxy as proxy
from services.community_common import community_in_tenant

forms_bp = Blueprint("v1_community_forms", __name__, url_prefix="/api/v1/admin")

#: Two-gate Feature flag -- `libs/community_module/features.py`'s
#: `community.forms` Feature contract, free tier.
FEATURE_COMMUNITY_FORMS = "waddles.community.forms"


@forms_bp.route("/<int:community_id>/forms", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.forms:read")  # type: ignore[misc]
async def list_forms(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/forms`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_FORMS, tenant=ctx.tenant_slug):
        return {"success": False, "error": "Community forms are not enabled for this plan"}, 402
    if not community_in_tenant(current_app.config["dal"], community_id, ctx):
        return {"success": False, "error": "Community not found"}, 404
    return await proxy.get_forms(community_id, request.headers.get("Authorization"))


@forms_bp.route("/<int:community_id>/forms/<int:form_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.forms:read")  # type: ignore[misc]
async def get_form(community_id: int, form_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/forms/<formId>`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not community_in_tenant(current_app.config["dal"], community_id, ctx):
        return {"success": False, "error": "Community not found"}, 404
    return await proxy.get_form(community_id, form_id, request.headers.get("Authorization"))


@forms_bp.route("/<int:community_id>/forms", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.forms:write")  # type: ignore[misc]
async def create_form(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/forms`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not community_in_tenant(current_app.config["dal"], community_id, ctx):
        return {"success": False, "error": "Community not found"}, 404
    payload = await request.get_json(force=True, silent=True) or {}
    return await proxy.create_form(community_id, payload, request.headers.get("Authorization"))


@forms_bp.route("/<int:community_id>/forms/<int:form_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.forms:write")  # type: ignore[misc]
async def delete_form(community_id: int, form_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/forms/<formId>`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not community_in_tenant(current_app.config["dal"], community_id, ctx):
        return {"success": False, "error": "Community not found"}, 404
    return await proxy.delete_form(community_id, form_id, request.headers.get("Authorization"))


@forms_bp.route("/<int:community_id>/forms/<int:form_id>/submissions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.forms:read")  # type: ignore[misc]
async def form_submissions(community_id: int, form_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/forms/<formId>/submissions`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not community_in_tenant(current_app.config["dal"], community_id, ctx):
        return {"success": False, "error": "Community not found"}, 404
    return await proxy.get_form_submissions(
        community_id, form_id, request.headers.get("Authorization")
    )


BLUEPRINTS: list[Blueprint] = [forms_bp]
