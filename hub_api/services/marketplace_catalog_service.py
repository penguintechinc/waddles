"""Marketplace catalog + module registry service -- port of `catalogService.js`/`moduleService.js`.

Two Node controllers, one service module (matching `hub_api/PORTING.md`'s
BUILD line for this group): `catalogController.js` (unified catalog
browsing, `marketplace_catalog` view) and `moduleController.js` (CRUD on
the `hub_modules` core registry). Both are read-mostly, low-write groups
that share no table but share the same `dal`-synchronous query style
already established by `services/community_announcements.py` -- no
`async_dal.*_async()` calls here, so none of `hub_api/PORTING.md`'s
Gotcha #1/#2/#3 (raw-SQL-is-Postgres-only, sqlite executor-thread
isolation, the `insert_async` + nested-dataclass crash) apply to this
group; every write below is a plain `dal.<table>.insert/update()`
followed by `dal.commit()`.

**SECURITY fix (faithful-port-but-fix-vulns, per this port's brief):**
Node's `catalogService.getCatalog/getCatalogEntry/getFeatured` query the
`marketplace_catalog` view with NO tenant filtering at all -- every
approved `marketplace_modules` row is visible to every caller regardless
of its `tenant_id` (added by `059_marketplace_consolidation.sql`,
backfilled to the global tenant only when previously unset). In a
multi-tenant deployment where a vendor's module submission is scoped to
one paying tenant (private/whitelabeled catalog), this leaks that
tenant's approved-but-private modules to every OTHER tenant, and to
anonymous callers. `visible_tenant_ids()` below closes this: every
caller (including anonymous, matching Node's `optionalAuth` -- catalog
browsing must keep working logged-out) always sees the global tenant's
catalog (`tenant_id IS NULL` -- core modules -- union `tenant_id ==
global_tenant.id`), and an authenticated caller additionally sees their
OWN tenant's submissions. A caller never sees a THIRD tenant's rows.
Already-approved-only filtering (`marketplace_modules.status =
'approved'`, `hub_modules.is_published = true`) is preserved unchanged
from the view definition itself -- pending/rejected/suspended modules
never appear in `marketplace_catalog` for anyone, faithfully matching
Node's existing (correct) behavior there.

`visible_tenant_ids()` mirrors `services/current_user.py`'s
`get_optional_current_user_id` precedent (an independent, self-contained
JWT re-decode tolerant of a missing/invalid token, not a new field
threaded through `flask_core.tenancy.TenantContext`) rather than
requiring `flask_core.tenancy.tenant_middleware`, which has no "optional"
mode (a missing bearer token is an unconditional 401) and would break
Node's logged-out browsing contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG, verify_jwt_token
from flask_core.secrets import require_secret_key

from services.errors import ApiError, bad_request, conflict, not_found

_MAX_LIMIT = 100


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _clamp_pagination(page: int, limit: int) -> tuple[int, int]:
    """Node's `Math.max(1, page)` / `Math.min(100, Math.max(1, limit))`, ported verbatim."""
    return max(1, page), min(_MAX_LIMIT, max(1, limit))


# ---------------------------------------------------------------------------
# Tenant-scoped catalog visibility -- see module docstring's SECURITY fix
# ---------------------------------------------------------------------------


def visible_tenant_ids(dal: Any, request: Any) -> frozenset[int]:
    """Tenant ids whose `marketplace_catalog` rows the caller may see.

    Always includes the global tenant (`DEFAULT_TENANT_SLUG`) -- the
    shared, public catalog every caller sees, logged in or not. A valid
    bearer JWT additionally adds the caller's own tenant. An
    absent/invalid/expired token is treated as "anonymous, global catalog
    only", matching Node's `optionalAuth` -- never an error.
    """
    visible: set[int] = set()

    global_row = dal(dal.tenants.slug == DEFAULT_TENANT_SLUG).select().first()
    if global_row is not None:
        visible.add(global_row.id)

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        secret_key = require_secret_key()
        payload = verify_jwt_token(token, secret_key)
        if payload:
            tenant_slug = payload.get("tenant")
            if tenant_slug:
                row = dal(dal.tenants.slug == tenant_slug).select().first()
                if row is not None and row.is_active:
                    visible.add(row.id)

    return frozenset(visible)


def _tenant_scope_query(dal: Any, visible_ids: frozenset[int]) -> Any:
    """`marketplace_catalog` rows visible to `visible_ids` -- core (NULL tenant) always included."""
    catalog = dal.marketplace_catalog
    if visible_ids:
        return (catalog.tenant_id == None) | catalog.tenant_id.belongs(visible_ids)  # noqa: E711
    return catalog.tenant_id == None  # noqa: E711


# ---------------------------------------------------------------------------
# Catalog DTOs + browsing -- port of catalogController.js
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    """One `marketplace_catalog` row -- camelCase DTO matching `formatCatalogEntry()`."""

    source: str
    sourceId: int
    name: str | None
    displayName: str | None
    description: str | None
    category: str | None
    iconUrl: str | None
    isCore: bool
    pricingType: str | None
    priceCents: int
    pricingModel: str | None
    version: str | None
    author: str | None
    communicationModel: str | None
    integrationType: str | None
    avgRating: str
    reviewCount: int
    installCount: int
    isInstalled: bool | None
    isEnabled: bool | None
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class CatalogPagination:
    """Page metadata for `GET /api/v1/marketplace/catalog`."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class CatalogListResponse:
    """Response DTO for `GET /api/v1/marketplace/catalog`."""

    success: bool
    modules: list[CatalogEntry]
    pagination: CatalogPagination


@dataclass(slots=True, frozen=True)
class CategoryEntry:
    """One distinct category + its module count."""

    category: str
    count: int


@dataclass(slots=True, frozen=True)
class CategoriesResponse:
    """Response DTO for `GET /api/v1/marketplace/catalog/categories`."""

    success: bool
    categories: list[CategoryEntry]


@dataclass(slots=True, frozen=True)
class FeaturedResponse:
    """Response DTO for `GET /api/v1/marketplace/catalog/featured`."""

    success: bool
    modules: list[CatalogEntry]


@dataclass(slots=True, frozen=True)
class CatalogEntryResponse:
    """Response DTO for `GET /api/v1/marketplace/catalog/<source>/<id>`."""

    success: bool
    module: CatalogEntry


def _install_status_maps(
    dal: Any, community_id: int | None, core_ids: list[int], marketplace_ids: list[int]
) -> tuple[dict[int, bool], dict[int, bool]]:
    """`{module_id: is_enabled}` for core installs and marketplace subscriptions.

    Two batch queries instead of Node's single per-row `LEFT JOIN` --
    portable across every `DB_TYPE` (`hub_api/PORTING.md` Gotcha #1) and
    sidesteps Gotcha #6's `left=` Row-shape ambiguity entirely. Catalog
    pages are capped at `_MAX_LIMIT` rows, so this is at most 2 extra
    queries per request, not N+1.
    """
    installed_core: dict[int, bool] = {}
    installed_marketplace: dict[int, bool] = {}
    if community_id is None:
        return installed_core, installed_marketplace

    if core_ids:
        rows = dal(
            (dal.hub_module_installations.community_id == community_id)
            & dal.hub_module_installations.module_id.belongs(core_ids)
        ).select(dal.hub_module_installations.module_id, dal.hub_module_installations.is_enabled)
        installed_core = {r.module_id: bool(r.is_enabled) for r in rows}

    if marketplace_ids:
        rows = dal(
            (dal.marketplace_subscriptions.community_id == community_id)
            & dal.marketplace_subscriptions.module_id.belongs(marketplace_ids)
        ).select(dal.marketplace_subscriptions.module_id, dal.marketplace_subscriptions.is_enabled)
        installed_marketplace = {r.module_id: bool(r.is_enabled) for r in rows}

    return installed_core, installed_marketplace


def _to_catalog_entry(
    row: Any, installed_core: dict[int, bool], installed_marketplace: dict[int, bool]
) -> CatalogEntry:
    is_installed: bool | None = None
    is_enabled: bool | None = None
    if row.source == "core":
        is_installed = row.source_id in installed_core
        is_enabled = installed_core.get(row.source_id)
    elif row.source == "marketplace":
        is_installed = row.source_id in installed_marketplace
        is_enabled = installed_marketplace.get(row.source_id)

    return CatalogEntry(
        source=row.source,
        sourceId=row.source_id,
        name=row.name,
        displayName=row.display_name,
        description=row.description,
        category=row.category,
        iconUrl=row.icon_url,
        isCore=bool(row.is_core),
        pricingType=row.pricing_type,
        priceCents=row.price_cents or 0,
        pricingModel=row.pricing_model,
        version=row.version,
        author=row.author,
        communicationModel=row.communication_model,
        integrationType=row.integration_type,
        avgRating=f"{float(row.avg_rating or 0):.1f}",
        reviewCount=int(row.review_count or 0),
        installCount=int(row.install_count or 0),
        isInstalled=is_installed,
        isEnabled=is_enabled,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


def get_catalog(
    dal: Any,
    request: Any,
    *,
    page: int,
    limit: int,
    search: str,
    category: str | None,
    pricing_type: str | None,
    source: str | None,
    community_id: int | None,
) -> CatalogListResponse:
    """Paginated, filtered, tenant-scoped catalog browse -- port of `getCatalog()`."""
    page, limit = _clamp_pagination(page, limit)
    catalog = dal.marketplace_catalog

    query = _tenant_scope_query(dal, visible_tenant_ids(dal, request))
    if search:
        pattern = f"%{search}%"
        query &= (
            catalog.name.like(pattern, case_sensitive=False)
            | catalog.display_name.like(pattern, case_sensitive=False)
            | catalog.description.like(pattern, case_sensitive=False)
        )
    if category:
        query &= catalog.category == category
    if pricing_type:
        query &= catalog.pricing_type == pricing_type
    if source:
        query &= catalog.source == source

    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=(~catalog.is_core, ~catalog.install_count, ~catalog.created_at),
        limitby=(offset, offset + limit),
    )

    core_ids = [r.source_id for r in rows if r.source == "core"]
    marketplace_ids = [r.source_id for r in rows if r.source == "marketplace"]
    installed_core, installed_marketplace = _install_status_maps(
        dal, community_id, core_ids, marketplace_ids
    )

    return CatalogListResponse(
        success=True,
        modules=[_to_catalog_entry(r, installed_core, installed_marketplace) for r in rows],
        pagination=CatalogPagination(
            page=page, limit=limit, total=total, totalPages=-(-total // limit) if limit else 0
        ),
    )


def get_catalog_entry(
    dal: Any, request: Any, source: str, source_id: int, community_id: int | None
) -> CatalogEntry | None:
    """Single catalog entry by `(source, sourceId)`, tenant-scoped -- port of `getCatalogEntry`."""
    catalog = dal.marketplace_catalog
    query = _tenant_scope_query(dal, visible_tenant_ids(dal, request))
    query &= (catalog.source == source) & (catalog.source_id == source_id)
    row = dal(query).select(limitby=(0, 1)).first()
    if row is None:
        return None

    installed_core, installed_marketplace = _install_status_maps(
        dal,
        community_id,
        [source_id] if source == "core" else [],
        [source_id] if source == "marketplace" else [],
    )
    return _to_catalog_entry(row, installed_core, installed_marketplace)


def get_categories(dal: Any) -> list[CategoryEntry]:
    """Distinct categories + counts, whole-catalog, unfiltered by tenant -- port of `getCategories`.

    Node's own `getCategories()` queries the raw view with no tenant
    filter either -- category NAMES are not tenant-sensitive data (unlike
    module identities/pricing), so this one endpoint is intentionally not
    tenant-scoped, matching the faithful port.
    """
    catalog = dal.marketplace_catalog
    # `marketplace_catalog` has no `id` field (composite `primarykey=`, see
    # `bind_marketplace_module_tables`'s own docstring) -- `source_id`
    # (never NULL) stands in as the COUNT() target. Sorted in Python
    # rather than via a pydal aggregate `orderby=` (the category list is
    # a handful of rows, and ordering by an aliased aggregate expression
    # is a fragile enough pydal corner to avoid rather than lean on).
    #
    # Row shape: selecting one real field (`category`) alongside an
    # aggregate expression on the SAME table nests the field under
    # `row.marketplace_catalog.category` and the alias under
    # `row._extra["module_count"]` -- confirmed empirically (a bare
    # `row.category` raises `AttributeError`), an extra wrinkle beyond
    # `hub_api/PORTING.md` Gotcha #6 (which only documents the multi-
    # table `left=` join case).
    rows = dal(catalog.category != None).select(  # noqa: E711
        catalog.category,
        catalog.source_id.count().with_alias("module_count"),
        groupby=catalog.category,
    )
    entries = [
        CategoryEntry(category=r.marketplace_catalog.category, count=int(r._extra["module_count"]))
        for r in rows
    ]
    entries.sort(key=lambda e: e.count, reverse=True)
    return entries


def get_featured(dal: Any, request: Any, community_id: int | None) -> list[CatalogEntry]:
    """Top 8 modules by install count then rating, tenant-scoped -- port of `getFeatured()`."""
    catalog = dal.marketplace_catalog
    query = _tenant_scope_query(dal, visible_tenant_ids(dal, request))
    rows = dal(query).select(orderby=(~catalog.install_count, ~catalog.avg_rating), limitby=(0, 8))
    core_ids = [r.source_id for r in rows if r.source == "core"]
    marketplace_ids = [r.source_id for r in rows if r.source == "marketplace"]
    installed_core, installed_marketplace = _install_status_maps(
        dal, community_id, core_ids, marketplace_ids
    )
    return [_to_catalog_entry(r, installed_core, installed_marketplace) for r in rows]


# ---------------------------------------------------------------------------
# Module registry DTOs + CRUD -- port of moduleController.js (hub_modules)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ModuleEntry:
    """One `hub_modules` row, camelCase to match `moduleService.js`'s `formatModule`."""

    id: int
    name: str
    displayName: str
    description: str | None
    version: str | None
    author: str | None
    category: str | None
    iconUrl: str | None
    isCore: bool
    isFeatured: bool
    avgRating: str
    reviewCount: int
    installCount: int
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class ModuleReview:
    """One `hub_module_reviews` row, camelCase to match `formatReview`."""

    id: int
    rating: int
    reviewText: str | None
    author: str
    authorAvatar: str | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ModuleListPagination:
    """Page metadata for `GET /api/v1/modules`."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class ModuleListResponse:
    """Response DTO for `GET /api/v1/modules`."""

    success: bool
    modules: list[ModuleEntry]
    pagination: ModuleListPagination


@dataclass(slots=True, frozen=True)
class ModuleDetail:
    """`getModuleById()`'s response shape -- `ModuleEntry` fields + `configSchema` + `reviews`."""

    id: int
    name: str
    displayName: str
    description: str | None
    version: str | None
    author: str | None
    category: str | None
    iconUrl: str | None
    isCore: bool
    isFeatured: bool
    avgRating: str
    reviewCount: int
    installCount: int
    createdAt: str | None
    updatedAt: str | None
    configSchema: dict[str, Any]
    reviews: list[ModuleReview] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ModuleDetailResponse:
    """Response DTO for `GET /api/v1/modules/<id>`."""

    success: bool
    module: ModuleDetail


@dataclass(slots=True, frozen=True)
class ModuleCreated:
    """`createModule()`'s response payload."""

    id: int
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ModuleCreateResponse:
    """Response DTO for `POST /api/v1/modules`."""

    success: bool
    message: str
    module: ModuleCreated


@dataclass(slots=True, frozen=True)
class SimpleMessageResponse:
    """Shared `{success, message}` response for update/delete."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class ModuleSubscription:
    """One community's installation of a module -- camelCase DTO, matches Node's response shape."""

    id: int
    communityId: int
    communityName: str | None
    communityDisplayName: str | None
    communityLogo: str | None
    isEnabled: bool
    config: dict[str, Any]
    installedAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class ModuleSubscriptionsResponse:
    """Response DTO for `GET /api/v1/modules/<id>/subscriptions`."""

    success: bool
    subscriptions: list[ModuleSubscription]
    total: int


def _to_module_entry(
    row: Any, avg_rating: float, review_count: int, install_count: int
) -> ModuleEntry:
    return ModuleEntry(
        id=row.id,
        name=row.name,
        displayName=row.display_name or row.name,
        description=row.description,
        version=row.version,
        author=row.author,
        category=row.category,
        iconUrl=row.icon_url,
        isCore=bool(row.is_core),
        isFeatured=bool(row.is_featured),
        avgRating=f"{avg_rating:.1f}",
        reviewCount=review_count,
        installCount=install_count,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


def _module_stats(dal: Any, module_id: int) -> tuple[float, int, int]:
    """`(avg_rating, review_count, install_count)` for one module -- `get_module`'s detail page."""
    review_rows = dal(dal.hub_module_reviews.module_id == module_id).select(
        dal.hub_module_reviews.rating
    )
    review_count = len(review_rows)
    avg_rating = (sum(r.rating for r in review_rows) / review_count) if review_count else 0.0
    install_count = dal(dal.hub_module_installations.module_id == module_id).count()
    return avg_rating, review_count, install_count


def _module_stats_batch(dal: Any, module_ids: list[int]) -> dict[int, tuple[float, int, int]]:
    """`{module_id: (avg_rating, review_count, install_count)}` for a whole page at once.

    `list_modules()`'s batched equivalent of `_module_stats()` -- 2 fixed
    queries per PAGE (grouped by `module_id`) instead of 2 per ROW, still
    entirely through the pydal query builder (`hub_api/PORTING.md`
    Gotcha #1 -- no raw SQL), no `LEFT JOIN`/Row-shape ambiguity
    (Gotcha #6) to reason about.
    """
    if not module_ids:
        return {}
    review_rows = dal(dal.hub_module_reviews.module_id.belongs(module_ids)).select(
        dal.hub_module_reviews.module_id, dal.hub_module_reviews.rating
    )
    install_rows = dal(dal.hub_module_installations.module_id.belongs(module_ids)).select(
        dal.hub_module_installations.module_id,
        dal.hub_module_installations.id.count().with_alias("install_count"),
        groupby=dal.hub_module_installations.module_id,
    )
    ratings: dict[int, list[int]] = {}
    for r in review_rows:
        ratings.setdefault(r.module_id, []).append(r.rating)
    # Field + aggregate on the same table nests under the tablename (see
    # `get_categories()`'s own comment on this same pydal Row shape).
    installs = {
        r.hub_module_installations.module_id: int(r._extra["install_count"]) for r in install_rows
    }

    stats: dict[int, tuple[float, int, int]] = {}
    for module_id in module_ids:
        rs = ratings.get(module_id, [])
        avg = (sum(rs) / len(rs)) if rs else 0.0
        stats[module_id] = (avg, len(rs), installs.get(module_id, 0))
    return stats


def list_modules(
    dal: Any, *, page: int, limit: int, search: str, category: str | None, featured: bool | None
) -> ModuleListResponse:
    """Paginated, published-only module browse -- port of `getModules()`."""
    page, limit = _clamp_pagination(page, limit)
    modules = dal.hub_modules

    query = modules.is_published == True  # noqa: E712
    if search:
        pattern = f"%{search}%"
        query &= (
            modules.name.like(pattern, case_sensitive=False)
            | modules.display_name.like(pattern, case_sensitive=False)
            | modules.description.like(pattern, case_sensitive=False)
        )
    if category:
        query &= modules.category == category
    if featured is not None:
        query &= modules.is_featured == featured

    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=(~modules.is_featured, ~modules.is_core, ~modules.created_at),
        limitby=(offset, offset + limit),
    )
    stats = _module_stats_batch(dal, [row.id for row in rows])
    entries = []
    for row in rows:
        avg_rating, review_count, install_count = stats.get(row.id, (0.0, 0, 0))
        entries.append(_to_module_entry(row, avg_rating, review_count, install_count))

    return ModuleListResponse(
        success=True,
        modules=entries,
        pagination=ModuleListPagination(
            page=page, limit=limit, total=total, totalPages=-(-total // limit) if limit else 0
        ),
    )


def get_module(dal: Any, module_id: int) -> ModuleDetail:
    """Full module detail + reviews, published only -- port of `getModuleById`. Raises 404."""
    modules = dal.hub_modules
    row = dal((modules.id == module_id) & (modules.is_published == True)).select().first()  # noqa: E712
    if row is None:
        raise not_found("Module not found")

    avg_rating, review_count, install_count = _module_stats(dal, module_id)
    review_rows = dal(dal.hub_module_reviews.module_id == module_id).select(
        orderby=~dal.hub_module_reviews.created_at, limitby=(0, 10)
    )
    reviews = [
        ModuleReview(
            id=r.id,
            rating=r.rating,
            reviewText=r.review_text,
            author="Anonymous",
            authorAvatar=None,
            createdAt=_iso(r.created_at),
        )
        for r in review_rows
    ]

    return ModuleDetail(
        id=row.id,
        name=row.name,
        displayName=row.display_name or row.name,
        description=row.description,
        version=row.version,
        author=row.author,
        category=row.category,
        iconUrl=row.icon_url,
        isCore=bool(row.is_core),
        isFeatured=bool(row.is_featured),
        avgRating=f"{avg_rating:.1f}",
        reviewCount=review_count,
        installCount=install_count,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
        configSchema=row.config_schema or {},
        reviews=reviews,
    )


def _validate_text(
    payload: dict[str, Any], key: str, *, min_len: int, max_len: int, required: bool
) -> str | None:
    """Port of Node's `validators.text` -- returns the trimmed value, or raises a 400 `ApiError`."""
    raw = payload.get(key)
    if raw is None or raw == "":
        if required:
            raise bad_request(f"{key} is required")
        return None
    if not isinstance(raw, str):
        raise bad_request(f"{key} must be a string")
    value = raw.strip()
    if not (min_len <= len(value) <= max_len):
        raise bad_request(f"{key} must be between {min_len} and {max_len} characters")
    return value


def _validate_url_field(payload: dict[str, Any], key: str) -> str | None:
    """Node's `validators.url` -- optional; a present-but-malformed URL is a 400."""
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not raw.startswith(("http://", "https://")):
        raise bad_request(f"{key} must be a valid URL")
    return raw.strip()


def create_module(dal: Any, payload: dict[str, Any], user_id: int) -> ModuleCreated:
    """Validate + insert a new `hub_modules` row -- port of `createModule()`. Raises `ApiError`."""
    name = _validate_text(payload, "name", min_len=3, max_len=100, required=True)
    display_name = _validate_text(payload, "displayName", min_len=3, max_len=255, required=False)
    description = _validate_text(payload, "description", min_len=0, max_len=5000, required=False)
    version = _validate_text(payload, "version", min_len=1, max_len=50, required=True)
    author = _validate_text(payload, "author", min_len=1, max_len=255, required=False)
    category = _validate_text(payload, "category", min_len=1, max_len=100, required=False)
    icon_url = _validate_url_field(payload, "iconUrl")
    is_core = bool(payload.get("isCore", False))
    is_featured = bool(payload.get("isFeatured", False))
    config_schema = payload.get("configSchema") or {}
    if not isinstance(config_schema, dict):
        raise bad_request("configSchema must be an object")

    if dal(dal.hub_modules.name == name).select(limitby=(0, 1)).first() is not None:
        raise conflict(f"A module named {name!r} already exists")

    now = datetime.utcnow()
    new_id = dal.hub_modules.insert(
        name=name,
        display_name=display_name,
        description=description,
        version=version,
        author=author,
        category=category,
        icon_url=icon_url,
        config_schema=config_schema,
        is_core=is_core,
        is_featured=is_featured,
        is_published=False,
        created_at=now,
        updated_at=now,
    )
    dal.commit()
    return ModuleCreated(id=new_id, createdAt=_iso(now))


_UPDATABLE_FIELDS = (
    "displayName",
    "description",
    "version",
    "author",
    "category",
    "iconUrl",
    "isFeatured",
    "isPublished",
)


def update_module(dal: Any, module_id: int, payload: dict[str, Any], user_id: int) -> None:
    """Partial update; only fields present in `payload` change -- port of `updateModule`."""
    modules = dal.hub_modules
    if dal(modules.id == module_id).select(limitby=(0, 1)).first() is None:
        raise not_found("Module not found")

    fields: dict[str, Any] = {}
    if "displayName" in payload:
        fields["display_name"] = _validate_text(
            payload, "displayName", min_len=3, max_len=255, required=False
        )
    if "description" in payload:
        fields["description"] = _validate_text(
            payload, "description", min_len=0, max_len=5000, required=False
        )
    if "version" in payload:
        fields["version"] = _validate_text(
            payload, "version", min_len=1, max_len=50, required=False
        )
    if "author" in payload:
        fields["author"] = _validate_text(payload, "author", min_len=1, max_len=255, required=False)
    if "category" in payload:
        fields["category"] = _validate_text(
            payload, "category", min_len=1, max_len=100, required=False
        )
    if "iconUrl" in payload:
        fields["icon_url"] = _validate_url_field(payload, "iconUrl")
    if "isFeatured" in payload:
        fields["is_featured"] = bool(payload["isFeatured"])
    if "isPublished" in payload:
        fields["is_published"] = bool(payload["isPublished"])

    if not fields:
        raise bad_request("No fields to update")

    fields["updated_at"] = datetime.utcnow()
    dal(modules.id == module_id).update(**fields)
    dal.commit()


def delete_module(dal: Any, module_id: int, user_id: int) -> None:
    """Delete a non-core module -- port of `deleteModule()`. Raises `ApiError`."""
    modules = dal.hub_modules
    row = dal((modules.id == module_id) & (modules.is_core == False)).select().first()  # noqa: E712
    if row is None:
        raise not_found("Module not found or cannot delete core module")
    dal(modules.id == module_id).delete()
    dal.commit()


def list_module_subscriptions(dal: Any, module_id: int) -> list[ModuleSubscription]:
    """Every community's installation of `module_id` -- port of `getModuleSubscriptions()`."""
    rows = dal(dal.hub_module_installations.module_id == module_id).select(
        orderby=~dal.hub_module_installations.installed_at
    )
    subscriptions = []
    for row in rows:
        community = dal(dal.communities.id == row.community_id).select().first()
        subscriptions.append(
            ModuleSubscription(
                id=row.id,
                communityId=row.community_id,
                communityName=community.name if community else None,
                communityDisplayName=community.display_name if community else None,
                communityLogo=community.logo_url if community else None,
                isEnabled=bool(row.is_enabled),
                config=row.config or {},
                installedAt=_iso(row.installed_at),
                updatedAt=_iso(row.updated_at),
            )
        )
    return subscriptions


__all__ = [
    "ApiError",
    "CatalogEntry",
    "CatalogEntryResponse",
    "CatalogListResponse",
    "CatalogPagination",
    "CategoriesResponse",
    "CategoryEntry",
    "FeaturedResponse",
    "ModuleCreateResponse",
    "ModuleCreated",
    "ModuleDetail",
    "ModuleDetailResponse",
    "ModuleEntry",
    "ModuleListPagination",
    "ModuleListResponse",
    "ModuleReview",
    "ModuleSubscription",
    "ModuleSubscriptionsResponse",
    "SimpleMessageResponse",
    "create_module",
    "delete_module",
    "get_catalog",
    "get_catalog_entry",
    "get_categories",
    "get_featured",
    "get_module",
    "list_module_subscriptions",
    "list_modules",
    "update_module",
    "visible_tenant_ids",
]
