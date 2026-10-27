"""v1 `marketplace.modules` group -- port of `moduleController.js` (CRUD on `hub_modules`).

Mounted at `/api/v1/modules`, matching Node's own `routes/modules.js`
mount point (`admin/marketplace_module/backend/src/index.js`:
`app.use('/api/v1', routes)` + `router.use('/modules', moduleRoutes)`) --
this group is not itself named in `admin/hub_module/frontend/src/
services/api.js`'s pinned contract (the frontend's module-CRUD surface
goes through the separate `superadminController.js`/`marketplaceController.js`
paths, out of this port's scope -- see `hub_api/PORTING.md`), so path
fidelity is to Node's OWN route table rather than a frontend consumer.

Auth pattern (`hub_api/PORTING.md` "Auth pattern" table): browse/detail
(`GET /`, `GET /<id>`) are public, matching Node's un-gated routes ahead
of `router.use(requireAuth)`. Every mutating route (create/update/delete)
plus the subscriptions-list route were `requireAuth` + `requireSuperAdmin`
in Node -- ported as `@tenant_middleware` + `@require_scope
("marketplace.modules:admin")`, the same single bundled `:admin` scope
`blueprints/v1/user_management.py` uses for its own superadmin-CRUD group
(`users:admin`), never a role-name check (security.md).
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.marketplace_catalog_service import (
    ModuleCreateResponse,
    ModuleDetailResponse,
    ModuleListResponse,
    ModuleSubscriptionsResponse,
    SimpleMessageResponse,
    create_module,
    delete_module,
    get_module,
    list_module_subscriptions,
    list_modules,
    update_module,
)

modules_bp = Blueprint("v1_marketplace_modules", __name__, url_prefix="/api/v1/modules")

_ADMIN_SCOPE = "marketplace.modules:admin"


def _dal() -> Any:
    """Return the app's synchronous pydal `DAL` -- this group never uses `async_dal`."""
    return current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@modules_bp.route("", methods=["GET"])
@validate_response(ModuleListResponse)
async def browse_modules() -> ModuleListResponse:
    """`GET /api/v1/modules` -- public, published-only browse."""
    dal = _dal()
    featured_param = request.args.get("featured")
    featured = None if featured_param is None else featured_param == "true"
    return list_modules(
        dal,
        page=max(1, _int_arg("page", 1)),
        limit=min(100, max(1, _int_arg("limit", 25))),
        search=request.args.get("search", ""),
        category=request.args.get("category") or None,
        featured=featured,
    )


@modules_bp.route("/<int:module_id>", methods=["GET"])
@validate_response(ModuleDetailResponse)
async def module_details(module_id: int) -> ModuleDetailResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/modules/<id>` -- public, published-only detail + reviews."""
    dal = _dal()
    try:
        detail = get_module(dal, module_id)
    except ApiError as exc:
        return _err(exc)
    return ModuleDetailResponse(success=True, module=detail)


@modules_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_ADMIN_SCOPE)  # type: ignore[misc]
# NOT @validate_response -- an `insert()` followed by a nested-dataclass
# response (`ModuleCreateResponse.module: ModuleCreated`) trips the same
# quart-schema/pydantic-core crash `services/dto_response.py`'s module
# docstring documents (confirmed here via a failing test: `TypeError:
# 'None' is not an instance of 'SchemaSerializer'` on `POST /api/v1/
# modules`) -- `hub_api/PORTING.md` Gotcha #3 only names `async_dal.
# insert_async()` as the trigger, but this group's plain sync `dal.
# hub_modules.insert()` reproduces it too. `jsonify_dto()` is the
# established workaround.
async def create_module_route() -> tuple[Any, int]:
    """`POST /api/v1/modules` -- superadmin only."""
    dal = _dal()
    user_id = get_current_user_id(request)
    payload = await request.get_json(force=True, silent=True) or {}
    try:
        created = create_module(dal, payload, user_id)
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        ModuleCreateResponse(success=True, message="Module created successfully", module=created),
        201,
    )


@modules_bp.route("/<int:module_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_ADMIN_SCOPE)  # type: ignore[misc]
@validate_response(SimpleMessageResponse)
async def update_module_route(
    module_id: int,
) -> SimpleMessageResponse | tuple[dict[str, object], int]:
    """`PUT /api/v1/modules/<id>` -- superadmin only."""
    dal = _dal()
    user_id = get_current_user_id(request)
    payload = await request.get_json(force=True, silent=True) or {}
    try:
        update_module(dal, module_id, payload, user_id)
    except ApiError as exc:
        return _err(exc)
    return SimpleMessageResponse(success=True, message="Module updated successfully")


@modules_bp.route("/<int:module_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_ADMIN_SCOPE)  # type: ignore[misc]
@validate_response(SimpleMessageResponse)
async def delete_module_route(
    module_id: int,
) -> SimpleMessageResponse | tuple[dict[str, object], int]:
    """`DELETE /api/v1/modules/<id>` -- superadmin only."""
    dal = _dal()
    user_id = get_current_user_id(request)
    try:
        delete_module(dal, module_id, user_id)
    except ApiError as exc:
        return _err(exc)
    return SimpleMessageResponse(success=True, message="Module deleted successfully")


@modules_bp.route("/<int:module_id>/subscriptions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_ADMIN_SCOPE)  # type: ignore[misc]
@validate_response(ModuleSubscriptionsResponse)
async def module_subscriptions(module_id: int) -> ModuleSubscriptionsResponse:
    """`GET /api/v1/modules/<id>/subscriptions` -- superadmin only."""
    dal = _dal()
    subscriptions = list_module_subscriptions(dal, module_id)
    return ModuleSubscriptionsResponse(
        success=True, subscriptions=subscriptions, total=len(subscriptions)
    )


BLUEPRINTS: list[Blueprint] = [modules_bp]
