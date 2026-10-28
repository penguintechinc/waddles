"""v1 `marketplace.admin_review` group -- admin review + internal integration.

Ports `adminReviewController.js`, the admin half of
`vendorRequestController.js`/`vendorSubmissionController.js` (see
`services/marketplace_review_service.py`'s module docstring), and
`routerIntegrationController.js`.

Two route surfaces:

- Admin (`/api/v1/marketplace/admin/marketplace/*`) -- `tenant_middleware`
  + `require_scope("marketplace:admin")`, the OIDC-native equivalent of
  Node's `requireSuperAdmin` boolean check (same precedent
  `blueprints/v1/user_management.py` documents for `users:admin`). Path +
  shape for `vendor-requests`/`submissions`/`settings` IDENTICAL to
  `admin/hub_module/frontend/src/services/api.js`'s pinned
  `marketplaceAdminApi` contract; `vendor-submissions/*` has no frontend
  contract (standalone pipeline, see the services module docstring) and
  is mounted alongside under the same admin prefix + scope.
- Internal (`/api/v1/internal/marketplace/*`) -- `X-Service-Key` only, no
  tenant/JWT, matches Node's `routes/internal.js`
  (`requireServiceAuth`) -- called by router/trigger modules, not end
  users. Mirrors `blueprints/v1/community_activity.py`'s own
  `activity_internal_bp` pattern (`services.community_common.
  is_valid_service_key`).

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request

from services import marketplace_command_service as commands
from services import marketplace_execution_service as execution
from services import marketplace_review_service as review
from services.community_common import api_error, is_valid_service_key
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.schema import bind_marketplace_vendor_tables

admin_review_bp = Blueprint(
    "v1_marketplace_admin_review", __name__, url_prefix="/api/v1/marketplace/admin/marketplace"
)
internal_bp = Blueprint(
    "v1_marketplace_internal", __name__, url_prefix="/api/v1/internal/marketplace"
)


def _dal() -> Any:
    dal = current_app.config["dal"]
    bind_marketplace_vendor_tables(dal)
    return dal


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _int_query_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, str(default)))
    except ValueError:
        return default


# ===== Vendor role requests (ports hub's vendorRequestController.js admin actions) =====


@admin_review_bp.route("/vendor-requests", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def get_vendor_requests() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/admin/marketplace/vendor-requests`."""
    dal = _dal()
    status = request.args.get("status")
    page = max(1, _int_query_arg("page", 1))
    limit = _int_query_arg("limit", 25)
    data = review.get_vendor_role_requests(dal, status=status, page=page, limit=limit)
    return {"success": True, **data}, 200


@admin_review_bp.route("/vendor-requests/<request_id>/approve", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def approve_vendor_request(request_id: str) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/admin/marketplace/vendor-requests/<id>/approve`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = review.approve_vendor_role_request(
            dal, request_id, admin_user_id=admin_user_id, admin_notes=body.get("notes", "")
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Vendor request approved", "request": result}, 200


@admin_review_bp.route("/vendor-requests/<request_id>/reject", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def reject_vendor_request(request_id: str) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/admin/marketplace/vendor-requests/<id>/reject`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = review.reject_vendor_role_request(
            dal,
            request_id,
            admin_user_id=admin_user_id,
            rejection_reason=body.get("reason", ""),
            admin_notes=body.get("notes", ""),
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Vendor request rejected", "request": result}, 200


# ===== marketplace_submissions review (adminReviewController.js) =====


@admin_review_bp.route("/submissions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def get_submissions() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/admin/marketplace/submissions`."""
    dal = _dal()
    status = request.args.get("status")
    page = max(1, _int_query_arg("page", 1))
    limit = _int_query_arg("limit", 25)
    data = review.get_submissions(dal, status=status, page=page, limit=limit)
    return {"success": True, **data}, 200


@admin_review_bp.route("/submissions/<int:submission_id>/approve", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def approve_submission(submission_id: int) -> tuple[dict[str, Any], int]:
    """`POST .../submissions/<id>/approve` -- self-approval-safe."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        review.approve_submission(
            dal, submission_id, admin_user_id=admin_user_id, notes=body.get("notes")
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Submission approved"}, 200


@admin_review_bp.route("/submissions/<int:submission_id>/reject", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def reject_submission(submission_id: int) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/admin/marketplace/submissions/<id>/reject`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        review.reject_submission(
            dal,
            submission_id,
            admin_user_id=admin_user_id,
            reason=body.get("reason"),
            notes=body.get("notes"),
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True}, 200


@admin_review_bp.route("/settings", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def get_marketplace_settings() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/admin/marketplace/settings`."""
    dal = _dal()
    settings = review.get_marketplace_settings(dal)
    return {"success": True, "settings": settings}, 200


@admin_review_bp.route("/settings", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def update_marketplace_settings() -> tuple[dict[str, Any], int]:
    """`PUT /api/v1/marketplace/admin/marketplace/settings`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    settings = body.get("settings", {})
    review.update_marketplace_settings(dal, settings, admin_user_id=admin_user_id)
    return {"success": True, "message": "Settings updated"}, 200


# ===== vendor_submissions pipeline admin actions (vendorSubmissionController.js) =====


@admin_review_bp.route("/vendor-submissions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def get_vendor_submissions_for_review() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/admin/marketplace/vendor-submissions`."""
    dal = _dal()
    status = request.args.get("status", "pending")
    page = max(1, _int_query_arg("page", 1))
    limit = min(50, max(1, _int_query_arg("limit", 20)))
    data = review.get_vendor_submissions_for_review(dal, status=status, page=page, limit=limit)
    return {"success": True, "data": data}, 200


@admin_review_bp.route("/vendor-submissions/<int:submission_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def get_vendor_submission_details(submission_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/admin/marketplace/vendor-submissions/<id>`."""
    dal = _dal()
    try:
        result = review.get_vendor_submission_details(dal, submission_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "submission": result}, 200


@admin_review_bp.route("/vendor-submissions/<int:submission_id>/approve", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def approve_vendor_submission(submission_id: int) -> tuple[dict[str, Any], int]:
    """`POST .../vendor-submissions/<id>/approve` -- self-approval-safe."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = review.approve_vendor_submission(
            dal, submission_id, admin_user_id=admin_user_id, admin_notes=body.get("adminNotes")
        )
    except ApiError as exc:
        return _err(exc)
    return {
        "success": True,
        "message": "Submission approved successfully",
        "submission": result,
    }, 200


@admin_review_bp.route("/vendor-submissions/<int:submission_id>/reject", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def reject_vendor_submission(submission_id: int) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/admin/marketplace/vendor-submissions/<id>/reject`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = review.reject_vendor_submission(
            dal,
            submission_id,
            admin_user_id=admin_user_id,
            rejection_reason=body.get("rejectionReason", ""),
            admin_notes=body.get("adminNotes"),
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Submission rejected", "submission": result}, 200


@admin_review_bp.route("/vendor-submissions/<int:submission_id>/request-info", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def request_more_info(submission_id: int) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/admin/marketplace/vendor-submissions/<id>/request-info`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        review.request_more_info(
            dal, submission_id, admin_user_id=admin_user_id, message=body.get("message", "")
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Information request sent to vendor"}, 200


@admin_review_bp.route("/vendor-submissions/<int:submission_id>/publish", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("marketplace:admin")  # type: ignore[misc]
async def publish_vendor_module(submission_id: int) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/admin/marketplace/vendor-submissions/<id>/publish`."""
    dal = _dal()
    admin_user_id = get_current_user_id(request)
    try:
        result = review.publish_vendor_module(dal, submission_id, admin_user_id=admin_user_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Module published to marketplace", "module": result}, 200


# ===== Internal (router-module) integration -- X-Service-Key only =====


@internal_bp.route("/commands/<int:community_id>", methods=["GET"])
async def get_community_commands(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/internal/marketplace/commands/<communityId>` -- service-to-service only."""
    if not is_valid_service_key(request):
        return api_error("Invalid service key", status_code=401)
    dal = _dal()
    result = commands.get_community_commands(dal, community_id)
    return {"success": True, "commands": result}, 200


@internal_bp.route("/execute/<int:module_id>", methods=["POST"])
async def execute_module_command(module_id: int) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/marketplace/execute/<moduleId>` -- service-to-service only."""
    if not is_valid_service_key(request):
        return api_error("Invalid service key", status_code=401)
    dal = _dal()
    payload = await request.get_json(force=True, silent=True) or {}
    execution.increment_request_count(dal, module_id)
    try:
        result = await execution.execute_command(dal, module_id, payload)
    except ApiError as exc:
        return _err(exc)
    return result, 200


BLUEPRINTS: list[Blueprint] = [admin_review_bp, internal_bp]
