"""v1 `marketplace.catalog` group -- port of `catalogController.js` (unified module browsing).

Mounted at `/api/v1/marketplace/catalog`, matching `admin/hub_module/
frontend/src/services/api.js`'s `unifiedMarketplaceApi` (`getCatalog`,
`getCatalogEntry`, `getCategories`, `getFeatured`) byte-for-byte -- see
`hub_api/PORTING.md`. Node's `routes/catalog.js` mounts all four routes
behind `optionalAuth` (public browsing, enriched install-status when a
community context is supplied) rather than `requireAuth` -- none of the
routes below carry `tenant_middleware`/`require_scope`, matching that
public-browse contract exactly.

`services/marketplace_catalog_service.py`'s module docstring documents
the SECURITY fix this group makes over the Node original: catalog
visibility is now tenant-scoped (global catalog + the caller's own
tenant, never a third tenant's private submissions) instead of Node's
unscoped view query.
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.api_utils import error_response
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.errors import ApiError
from services.marketplace_catalog_service import (
    CatalogEntryResponse,
    CatalogListResponse,
    CategoriesResponse,
    FeaturedResponse,
    VendorsResponse,
    get_catalog,
    get_catalog_bundle_entry,
    get_catalog_entry,
    get_categories,
    get_featured,
    get_vendors,
)

catalog_bp = Blueprint("v1_marketplace_catalog", __name__, url_prefix="/api/v1/marketplace/catalog")
vendors_bp = Blueprint("v1_marketplace_vendors", __name__, url_prefix="/api/v1/marketplace/vendors")


def _dal() -> Any:
    """Return the app's synchronous pydal `DAL` -- this group never uses `async_dal`."""
    return current_app.config["dal"]


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _optional_int_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@catalog_bp.route("", methods=["GET"])
@validate_response(CatalogListResponse)
async def browse_catalog() -> CatalogListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/marketplace/catalog` -- paginated browsing with search & filters.

    `vendor` (GitHub org key, or `_unaffiliated`) and `repo` (`org/repo`) narrow to one vendor
    marketplace / one repo inside it; malformed values are a 400, never ignored.
    """
    dal = _dal()
    try:
        return get_catalog(
            dal,
            request,
            page=max(1, _int_arg("page", 1)),
            limit=min(100, max(1, _int_arg("limit", 25))),
            search=request.args.get("search", ""),
            category=request.args.get("category") or None,
            pricing_type=request.args.get("pricingType") or None,
            source=request.args.get("source") or None,
            community_id=_optional_int_arg("communityId"),
            vendor=request.args.get("vendor") or None,
            repo=request.args.get("repo") or None,
        )
    except ApiError as exc:
        return cast(
            tuple[dict[str, object], int],
            error_response(exc.message, status_code=exc.status_code, error_code=exc.code),
        )


@vendors_bp.route("", methods=["GET"])
@validate_response(VendorsResponse)
async def vendors() -> VendorsResponse:
    """`GET /api/v1/marketplace/vendors` -- vendor marketplaces (GitHub orgs) with repo counts."""
    return get_vendors(_dal(), request)


@catalog_bp.route("/categories", methods=["GET"])
@validate_response(CategoriesResponse)
async def categories() -> CategoriesResponse:
    """`GET /api/v1/marketplace/catalog/categories` -- distinct categories with module counts."""
    dal = _dal()
    return CategoriesResponse(success=True, categories=get_categories(dal))


@catalog_bp.route("/featured", methods=["GET"])
@validate_response(FeaturedResponse)
async def featured() -> FeaturedResponse:
    """`GET /api/v1/marketplace/catalog/featured` -- top/featured modules."""
    dal = _dal()
    modules = get_featured(dal, request, _optional_int_arg("communityId"))
    return FeaturedResponse(success=True, modules=modules)


@catalog_bp.route("/bundle/<string:app_id>", methods=["GET"])
@validate_response(CatalogEntryResponse)
async def catalog_bundle_entry(app_id: str) -> CatalogEntryResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/marketplace/catalog/bundle/<app_id>` -- single bundle entry (by app_id)."""
    entry = get_catalog_bundle_entry(_dal(), request, app_id, _optional_int_arg("communityId"))
    if entry is None:
        return cast(
            tuple[dict[str, object], int],
            error_response("Module not found", status_code=404, error_code="NOT_FOUND"),
        )
    return CatalogEntryResponse(success=True, module=entry)


@catalog_bp.route("/<string:source>/<int:source_id>", methods=["GET"])
@validate_response(CatalogEntryResponse)
async def catalog_entry(
    source: str, source_id: int
) -> CatalogEntryResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/marketplace/catalog/<source>/<id>` -- single catalog entry detail."""
    dal = _dal()
    entry = get_catalog_entry(dal, request, source, source_id, _optional_int_arg("communityId"))
    if entry is None:
        return cast(
            tuple[dict[str, object], int],
            error_response("Module not found", status_code=404, error_code="NOT_FOUND"),
        )
    return CatalogEntryResponse(success=True, module=entry)


BLUEPRINTS: list[Blueprint] = [catalog_bp, vendors_bp]
