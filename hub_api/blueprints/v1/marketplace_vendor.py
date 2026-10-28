"""v1 `marketplace.vendor` group -- vendor self-service + public submission intake.

Ports `vendorController.js` + `vendorAnalyticsController.js`
(marketplace_module) and the public submission pipeline of
`vendorSubmissionController.js` (hub_module).

Two route surfaces:

- Self-service (`/api/v1/marketplace/vendor/*`) -- `tenant_middleware`
  only + `services.current_user.get_current_user_id`, matching
  `hub_api/PORTING.md`'s "Self-service (caller acting on their OWN
  resource)" auth-pattern row. Path + method IDENTICAL to
  `admin/hub_module/frontend/src/services/api.js`'s pinned `vendorApi`
  contract (`/api/v1/marketplace/vendor/...`).
- Public (`/api/v1/marketplace/public/vendor/*`) -- no auth at all,
  matches Node's own unauthenticated `POST /vendor/submit` /
  `GET /vendor/submissions/:id` / `GET /vendor/modules` (this pipeline
  has no frontend contract in `api.js` -- see `services/
  marketplace_review_service.py`'s module docstring for the parallel-
  pipeline context).

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, Response, current_app, jsonify, request
from quart_schema import validate_response

from services import marketplace_analytics_service as analytics
from services import marketplace_review_service as review
from services import marketplace_vendor_service as vendor
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.schema import bind_marketplace_vendor_tables

vendor_bp = Blueprint("v1_marketplace_vendor", __name__, url_prefix="/api/v1/marketplace/vendor")
vendor_public_bp = Blueprint(
    "v1_marketplace_vendor_public", __name__, url_prefix="/api/v1/marketplace/public/vendor"
)


@dataclass(slots=True, frozen=True)
class SellerDTO:
    """Vendor's own seller row (payout/revenue = financial data)."""

    id: int
    userId: int
    displayName: str
    description: str | None
    websiteUrl: str | None
    payoutMethod: str | None
    totalRevenueCents: int
    totalSubscribers: int
    isVerified: bool
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class SellerResponse:
    """`{success, seller}` envelope for the vendor-profile routes."""

    success: bool
    seller: SellerDTO | None


@dataclass(slots=True, frozen=True)
class VendorRequestDTO:
    """Vendor-role request as its requester sees it (no contact PII)."""

    id: int
    requestId: str | None = None
    status: str | None = None
    companyName: str | None = None
    rejectionReason: str | None = None
    requestedAt: str | None = None
    reviewedAt: str | None = None


@dataclass(slots=True, frozen=True)
class VendorRequestResponse:
    """`{success, request}` envelope for the vendor-request routes."""

    success: bool
    request: VendorRequestDTO | None
    message: str | None = None


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


# ===== Vendor profile =====


@vendor_bp.route("/profile", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(SellerResponse, 200)
async def get_vendor_profile() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/profile`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    seller = vendor.get_vendor_profile(dal, user_id)
    return {"success": True, "seller": seller}, 200


@vendor_bp.route("/profile", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(SellerResponse, 201)
async def create_vendor_profile() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/vendor/profile`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        seller = vendor.create_vendor_profile(dal, user_id, body)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "seller": seller}, 201


@vendor_bp.route("/profile", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_response(SellerResponse, 200)
async def update_vendor_profile() -> tuple[dict[str, Any], int]:
    """`PUT /api/v1/marketplace/vendor/profile`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        vendor.validate_vendor_profile_input(body)
        seller = vendor.update_vendor_profile(dal, user_id, body)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "seller": seller}, 200


# ===== Dashboard =====


@vendor_bp.route("/dashboard", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_vendor_dashboard() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/dashboard`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    try:
        data = vendor.get_vendor_dashboard(dal, user_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, **data}, 200


# ===== Analytics (registered before /analytics/overview -- matches Node's comment
# about path-conflict ordering; quart resolves by exact-match so ordering has no
# functional effect here, kept for readability parity with vendor.js) =====


@vendor_bp.route("/analytics/sales", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_sales_metrics() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/analytics/sales`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    period = request.args.get("period", "30d")
    data = analytics.get_sales_metrics(dal, user_id, period=period)
    if data is None:
        return {"success": False, "error": "Vendor profile not found"}, 404
    return {"success": True, "data": data}, 200


@vendor_bp.route("/analytics/installs", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_install_time_series() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/analytics/installs`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    period = request.args.get("period", "30d")
    granularity = request.args.get("granularity", "day")
    data = analytics.get_install_time_series(dal, user_id, period=period, granularity=granularity)
    return {"success": True, "data": data}, 200


@vendor_bp.route("/analytics/api-usage", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_api_usage_metrics() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/analytics/api-usage`."""
    get_current_user_id(request)  # auth check only, matches Node (userId unused downstream)
    period = request.args.get("period", "30d")
    data = analytics.get_api_usage_metrics(period=period)
    return {"success": True, "data": data}, 200


@vendor_bp.route("/analytics/discount-codes", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_discount_code_performance() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/analytics/discount-codes`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    data = analytics.get_discount_code_performance(dal, user_id)
    return {"success": True, "data": data}, 200


@vendor_bp.route("/analytics/communities", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_community_drilldown() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/analytics/communities`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    module_id_raw = request.args.get("moduleId")
    module_id = None
    if module_id_raw:
        try:
            module_id = int(module_id_raw)
            if module_id <= 0:
                raise ValueError
        except ValueError:
            return {"success": False, "error": "Invalid moduleId"}, 400
    page = max(1, _int_query_arg("page", 1))
    limit = min(100, max(1, _int_query_arg("limit", 25)))
    sort_by = request.args.get("sortBy", "installed_at")
    data = analytics.get_community_drilldown(
        dal, user_id, module_id=module_id, page=page, limit=limit, sort_by=sort_by
    )
    return {"success": True, "data": data}, 200


@vendor_bp.route("/analytics/export", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def export_analytics_csv() -> Response:
    """`GET /api/v1/marketplace/vendor/analytics/export`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    export_type = request.args.get("type", "sales")
    if export_type not in ("sales", "installs"):
        response = Response("Invalid type. Must be one of: sales, installs")
        response.status_code = 400
        return response
    period = request.args.get("period", "30d")
    csv_text, filename = analytics.export_analytics_csv(
        dal, user_id, export_type=export_type, period=period
    )
    response = Response(csv_text, content_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@vendor_bp.route("/analytics/overview", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_vendor_analytics_overview() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/analytics/overview`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    try:
        data = vendor.get_vendor_analytics_overview(dal, user_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "analytics": data}, 200


# ===== Modules =====


@vendor_bp.route("/modules", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_vendor_modules() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/modules`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    page = max(1, _int_query_arg("page", 1))
    limit = _int_query_arg("limit", 25)
    data = vendor.get_vendor_modules(dal, user_id, page=page, limit=limit)
    return {"success": True, **data}, 200


@vendor_bp.route("/modules", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def create_vendor_module() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/vendor/modules`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        module = vendor.create_vendor_module(dal, user_id, body)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "module": module}, 201


@vendor_bp.route("/modules/<int:module_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
async def update_vendor_module(module_id: int) -> tuple[dict[str, Any], int]:
    """`PUT /api/v1/marketplace/vendor/modules/<id>` -- IDOR-safe (own modules only)."""
    dal = _dal()
    user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = vendor.update_vendor_module(dal, user_id, module_id, body)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, **result}, 200


@vendor_bp.route("/modules/<int:module_id>/submit", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def submit_module_for_review(
    module_id: int,
) -> tuple[dict[str, Any], int] | tuple[Response, int]:
    """`POST /api/v1/marketplace/vendor/modules/<id>/submit` -- IDOR-safe.

    Response wrapped in `jsonify()` (NOT a bare dict tuple) -- a flat dict
    returned here after `vendor.submit_module_for_review`'s
    `dal.insert()`/`dal.update()` writes reproducibly hit the
    `TypeError: 'None' is not an instance of 'SchemaSerializer'` crash
    `services/dto_response.py` documents for nested-dataclass responses
    (`hub_api/PORTING.md` Gotcha #3) -- confirmed here on a PLAIN dict
    too under this same request chain (list -> update -> submit in one
    test), broadening that gotcha's known trigger surface. `jsonify()`
    is the general form of the same workaround `jsonify_dto()` applies
    to dataclasses (see that module's docstring: a real `quart.Response`
    falls through quart-schema's `TypeAdapter` path untouched).
    """
    dal = _dal()
    user_id = get_current_user_id(request)
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = vendor.submit_module_for_review(
            dal, user_id, module_id, body.get("changesDescription")
        )
    except ApiError as exc:
        return _err(exc)
    return (
        jsonify(
            {
                "success": True,
                "submissionId": result["submissionId"],
                "message": "Module submitted for review",
            }
        ),
        200,
    )


# ===== Vendor role request =====


@vendor_bp.route("/request", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(VendorRequestResponse, 200)
async def get_vendor_request() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/vendor/request`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    result = vendor.get_vendor_request(dal, user_id)
    return {"success": True, "request": result}, 200


@vendor_bp.route("/request", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(VendorRequestResponse, 201)
async def create_vendor_request() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/vendor/request`."""
    dal = _dal()
    user_id = get_current_user_id(request)
    hub_user = dal.hub_users[user_id]
    user_email = hub_user.email if hub_user else ""
    user_display_name = hub_user.display_name if hub_user else None
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = vendor.create_vendor_request(dal, user_id, user_email, user_display_name, body)
    except ApiError as exc:
        return _err(exc)
    return {
        "success": True,
        "message": "Vendor request submitted successfully",
        "request": result,
    }, 201


# ===== Public vendor-submission pipeline (no frontend contract; see module docstring) =====


@vendor_public_bp.route("/submit", methods=["POST"])
async def submit_vendor_module() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/marketplace/public/vendor/submit` -- unauthenticated."""
    dal = _dal()
    body = await request.get_json(force=True, silent=True) or {}
    try:
        result = review.submit_vendor_module(dal, body)
    except ApiError as exc:
        return _err(exc)
    return {
        "success": True,
        "message": "Module submission received successfully",
        "submission": result,
    }, 201


@vendor_public_bp.route("/submissions/<submission_id>", methods=["GET"])
async def get_submission_status(submission_id: str) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/public/vendor/submissions/<id>?email=` -- IDOR-safe."""
    dal = _dal()
    email = request.args.get("email")
    if not email:
        return {"success": False, "message": "Vendor email required"}, 400
    try:
        result = review.get_submission_status(dal, submission_id, email)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "submission": result}, 200


@vendor_public_bp.route("/modules", methods=["GET"])
async def get_published_modules() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/marketplace/public/vendor/modules` -- unauthenticated listing."""
    dal = _dal()
    page = max(1, _int_query_arg("page", 1))
    limit = min(100, max(1, _int_query_arg("limit", 20)))
    featured = request.args.get("featured") == "true"
    data = review.get_published_modules(dal, page=page, limit=limit, featured=featured)
    return {"success": True, "data": data}, 200


BLUEPRINTS: list[Blueprint] = [vendor_bp, vendor_public_bp]
