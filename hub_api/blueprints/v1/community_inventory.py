"""v1 `community.inventory` group -- port of Node's `inventoryController.js` (M6 Community).

Admin routes (item CRUD, stock adjustments, checkout list, summary, audit
log) and member routes (browse available, checkout, checkin, my-items),
mounted under `/api/v1/admin` matching Node's `routes/inventory.js`. See
`services/community_inventory.py`'s module docstring for the
checkout/checkin bug fixed during this port.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from flask_core.api_utils import auth_required
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services.community_common import (
    api_error,
    community_in_tenant,
    feature_not_enabled,
    get_current_user,
)
from services.community_inventory import (
    add_stock,
    checkin_item,
    checkout_item,
    create_item,
    delete_item,
    get_audit_log,
    get_my_checkouts,
    get_summary,
    list_all_checkouts,
    list_available,
    list_items,
    remove_stock,
    update_item,
)
from services.community_inventory_authz import require_inventory_admin, require_inventory_member
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.pagination import parse_limit
from services.public_ids import (
    PublicIdTarget,
    Scope,
    build_view_arg_translator,
    resolve_public_id_sync,
)
from services.route_guards import GUARD_ADMIN, mark_guarded

logger = logging.getLogger(__name__)

inventory_bp = Blueprint("v1_community_inventory", __name__, url_prefix="/api/v1/admin")

#: `<puuid:item_id>` -> numeric id inside the URL community (unknown/foreign/malformed -> 404).
inventory_bp.before_request(
    build_view_arg_translator({"item_id": PublicIdTarget("inventory_items")})
)

#: Two-gate Feature flag -- `libs/community_module/features.py`'s
#: `community.inventory` Feature contract, Professional tier.
FEATURE_COMMUNITY_INVENTORY = "waddles.community.inventory"


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


async def _precheck(community_id: int) -> tuple[Any, tuple[dict[str, object], int] | None]:
    """Feature flag (402) -> tenant<->community (404). Returns `(ctx, denial_or_None)`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_INVENTORY, tenant=ctx.tenant_slug):
        return ctx, feature_not_enabled(
            "Community inventory requires a Professional plan or higher"
        )
    if not _tenant_ok(community_id):
        return ctx, api_error("Community not found", status_code=404)
    return ctx, None


async def _guard_admin(community_id: int) -> tuple[dict[str, object], int] | None:
    """Precheck, then 403 unless the caller administers THIS community (never via `*:read`)."""
    ctx, denied = await _precheck(community_id)
    if denied:
        return denied
    dal = current_app.config["dal"]
    try:
        require_inventory_admin(
            dal, ctx, community_id=community_id, user_id=get_current_user_id(request)
        )
    except ApiError as exc:
        logger.debug("inventory guard denied community=%s status=%s", community_id, exc.status_code)
        return api_error(exc.message, exc.status_code)
    return None


async def _guard_member(community_id: int) -> tuple[dict[str, object], int] | None:
    """Precheck, then 403 unless the caller is an active, unbanned member of THIS community."""
    ctx, denied = await _precheck(community_id)
    if denied:
        return denied
    dal = current_app.config["dal"]
    try:
        require_inventory_member(
            dal, ctx, community_id=community_id, user_id=get_current_user_id(request)
        )
    except ApiError as exc:
        logger.debug("inventory guard denied community=%s status=%s", community_id, exc.status_code)
        return api_error(exc.message, exc.status_code)
    return None


async def _guard(community_id: int, *, admin: bool) -> tuple[dict[str, object], int] | None:
    """Admin or member guard by flag; stamped admin routes call `_guard_admin` directly."""
    return await (_guard_admin(community_id) if admin else _guard_member(community_id))


# ── Admin routes ──────────────────────────────────────────────────────────


@inventory_bp.route("/<cuuid:community_id>/inventory/items", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:read")  # type: ignore[misc]
@mark_guarded(GUARD_ADMIN)  # type: ignore[misc]
async def list_items_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/inventory/items`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    items = list_items(current_app.config["dal"], community_id)
    return {"items": [asdict(i) for i in items]}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/items", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:write")  # type: ignore[misc]
async def create_item_route(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/inventory/items`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    dto, err = create_item(current_app.config["dal"], community_id, payload)
    if err:
        return {"error": err}, 400
    assert dto is not None  # nosec B101 -- create_item: dto is None only when err is set
    return {"item": asdict(dto)}, 201


@inventory_bp.route("/<cuuid:community_id>/inventory/items/<puuid:item_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:write")  # type: ignore[misc]
async def update_item_route(community_id: int, item_id: int) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/inventory/items/<itemId>`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    dto = update_item(current_app.config["dal"], community_id, item_id, payload)
    if dto is None:
        return {"error": "Item not found"}, 404
    return {"item": asdict(dto)}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/items/<puuid:item_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:write")  # type: ignore[misc]
async def delete_item_route(community_id: int, item_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/inventory/items/<itemId>`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    if not delete_item(current_app.config["dal"], community_id, item_id):
        return {"error": "Item not found"}, 404
    return {"message": "Item deleted"}, 200


@inventory_bp.route(
    "/<cuuid:community_id>/inventory/items/<puuid:item_id>/stock/add", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def add_stock_route(community_id: int, item_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/inventory/items/<itemId>/stock/add`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    try:
        quantity = int(payload.get("quantity"))  # type: ignore[arg-type]  # TypeError caught below
    except (TypeError, ValueError):
        return {"error": "Quantity must be a positive number"}, 400
    if quantity <= 0:
        return {"error": "Quantity must be a positive number"}, 400

    dto, err = add_stock(
        current_app.config["dal"],
        community_id,
        item_id,
        int(get_current_user(request)["user_id"]),
        quantity,
        payload.get("notes"),
    )
    if err:
        return {"error": err}, 404 if "not found" in err.lower() else 400
    assert dto is not None  # nosec B101 -- add_stock: dto is None only when err is set
    return {"item": asdict(dto)}, 200


@inventory_bp.route(
    "/<cuuid:community_id>/inventory/items/<puuid:item_id>/stock/remove", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def remove_stock_route(community_id: int, item_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/inventory/items/<itemId>/stock/remove`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    try:
        quantity = int(payload.get("quantity"))  # type: ignore[arg-type]  # TypeError caught below
    except (TypeError, ValueError):
        return {"error": "Quantity must be a positive number"}, 400
    if quantity <= 0:
        return {"error": "Quantity must be a positive number"}, 400

    dto, err = remove_stock(
        current_app.config["dal"],
        community_id,
        item_id,
        int(get_current_user(request)["user_id"]),
        quantity,
        payload.get("notes"),
    )
    if err:
        return {"error": err}, 404 if "not found" in err.lower() else 400
    assert dto is not None  # nosec B101 -- remove_stock: dto is None only when err is set
    return {"item": asdict(dto)}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/checkouts", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:read")  # type: ignore[misc]
@mark_guarded(GUARD_ADMIN)  # type: ignore[misc]
async def list_checkouts_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/inventory/checkouts`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    checkouts = list_all_checkouts(
        current_app.config["dal"], community_id, request.args.get("status")
    )
    return {"checkouts": [asdict(c) for c in checkouts]}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/summary", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:read")  # type: ignore[misc]
@mark_guarded(GUARD_ADMIN)  # type: ignore[misc]
async def summary_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/inventory/summary`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    return {"summary": asdict(get_summary(current_app.config["dal"], community_id))}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/log", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.inventory:read")  # type: ignore[misc]
@mark_guarded(GUARD_ADMIN)  # type: ignore[misc]
async def audit_log_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/inventory/log`."""
    denied = await _guard_admin(community_id)
    if denied:
        return denied
    item_id: int | None = None
    raw_item = request.args.get("item_id")
    if raw_item:
        item_id = resolve_public_id_sync(
            current_app.config["dal"],
            "inventory_items",
            raw_item,
            scope=Scope(community_id=community_id),
        )
        if item_id is None:
            return {"error": "Item not found"}, 404
    limit = parse_limit(request.args.get("limit"), default=50)
    offset = request.args.get("offset", default=0, type=int)
    log = get_audit_log(
        current_app.config["dal"],
        community_id,
        item_id=item_id,
        action=request.args.get("action"),
        limit=limit,
        offset=offset,
    )
    return {"log": [asdict(entry) for entry in log]}, 200


# ── Member routes ─────────────────────────────────────────────────────────


@inventory_bp.route("/<cuuid:community_id>/inventory/available", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def available_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/inventory/available`."""
    denied = await _guard(community_id, admin=False)
    if denied:
        return denied
    items = list_available(current_app.config["dal"], community_id, request.args.get("search"))
    return {"items": [asdict(i) for i in items]}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/checkout", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def checkout_route(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/inventory/checkout`."""
    denied = await _guard(community_id, admin=False)
    if denied:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    raw_item_id = payload.get("item_id")
    if not raw_item_id:
        return {"error": "item_id is required"}, 400
    try:
        quantity = int(payload.get("quantity"))  # type: ignore[arg-type]  # TypeError caught below
    except (TypeError, ValueError):
        return {"error": "Quantity must be a positive number"}, 400
    if quantity <= 0:
        return {"error": "Quantity must be a positive number"}, 400
    item_id = resolve_public_id_sync(
        current_app.config["dal"],
        "inventory_items",
        raw_item_id,
        scope=Scope(community_id=community_id),
    )
    if item_id is None:
        return {"error": "Item not found"}, 404

    dto, err = checkout_item(
        current_app.config["dal"],
        community_id,
        int(get_current_user(request)["user_id"]),
        item_id,
        quantity,
        payload.get("due_date"),
        payload.get("notes"),
    )
    if err:
        status = (
            404 if "not found" in err.lower() else 409 if "insufficient" in err.lower() else 400
        )
        return {"error": err}, status
    assert dto is not None  # nosec B101 -- checkout_item: dto is None only when err is set
    return {"checkout": asdict(dto)}, 201


@inventory_bp.route("/<cuuid:community_id>/inventory/checkin", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def checkin_route(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/inventory/checkin`."""
    denied = await _guard(community_id, admin=False)
    if denied:
        return denied
    payload = await request.get_json(force=True, silent=True) or {}
    checkout_id = payload.get("checkout_id")
    if not checkout_id:
        return {"error": "checkout_id is required"}, 400
    raw_qty = payload.get("quantity_returned")
    quantity_returned: int | None = None
    if raw_qty is not None:
        try:
            quantity_returned = int(raw_qty)
        except (TypeError, ValueError):
            return {"error": "quantity_returned must be a positive number"}, 400
        if isinstance(raw_qty, bool) or quantity_returned <= 0:
            return {"error": "quantity_returned must be a positive number"}, 400

    dto, err = checkin_item(
        current_app.config["dal"],
        community_id,
        int(get_current_user(request)["user_id"]),
        int(checkout_id),
        quantity_returned,
        payload.get("notes"),
    )
    if err:
        return {"error": err}, 404 if "not found" in err.lower() else 400
    assert dto is not None  # nosec B101 -- checkin_item: dto is None only when err is set
    return {"checkout": asdict(dto)}, 200


@inventory_bp.route("/<cuuid:community_id>/inventory/my-items", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def my_items_route(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/inventory/my-items`."""
    denied = await _guard(community_id, admin=False)
    if denied:
        return denied
    checkouts = get_my_checkouts(
        current_app.config["dal"], community_id, int(get_current_user(request)["user_id"])
    )
    return {"checkouts": [asdict(c) for c in checkouts]}, 200


BLUEPRINTS: list[Blueprint] = [inventory_bp]
