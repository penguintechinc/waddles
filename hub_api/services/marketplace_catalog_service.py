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

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG, verify_jwt_token
from flask_core.bundle_author import author_view
from flask_core.secrets import require_secret_key

from services.errors import bad_request
from services.marketplace_author import AuthorDTO, to_author_dto

_MAX_LIMIT = 100

logger = logging.getLogger(__name__)

#: Filter value selecting rows with no derivable vendor (legacy/plain-name authors). Contains an
#: underscore, which a GitHub org name can never contain, so it cannot collide with a real vendor.
UNAFFILIATED_VENDOR = "_unaffiliated"
_VENDOR_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?|_unaffiliated)$")
_REPO_RE = re.compile(r"^[a-z0-9-]{1,100}/[a-z0-9._-]{1,150}$")


#: app_ids already reported as vendorless in this process -- one ERROR per distinct bundle, not
#: one per request.
_VENDORLESS_LOGGED: set[str] = set()


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
    sourceId: int | None
    sourceRef: str | None
    vendorKey: str | None
    vendorName: str | None
    sourceRepo: str | None
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
    author: AuthorDTO | None
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
class VendorRepoEntry:
    """One source repo (`org/repo`) inside a vendor marketplace + its entry count."""

    repo: str | None
    count: int


@dataclass(slots=True, frozen=True)
class VendorEntry:
    """One vendor marketplace (a GitHub org) with its per-repo breakdown."""

    key: str
    name: str
    count: int
    repos: list[VendorRepoEntry]


@dataclass(slots=True, frozen=True)
class VendorsResponse:
    """Response DTO for `GET /api/v1/marketplace/vendors`; counts sum to `total`."""

    success: bool
    vendors: list[VendorEntry]
    total: int


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


def _vendor_of(row: Any) -> tuple[str | None, str | None]:
    """`(vendor_key, display_name)` for a catalog row; `(None, None)` when no vendor derives.

    `vendor_key` is derived in the view (alembic 0091). A vendorless `bundle` row is listed
    as unaffiliated (never a 500, never hidden) and reported once per app_id at ERROR. The
    display name keeps the GitHub handle's original casing from `author` when it matches the
    key, else falls back to the key.
    """
    key = row.vendor_key
    if not key:
        if row.source == "bundle" and row.source_ref not in _VENDORLESS_LOGGED:
            _VENDORLESS_LOGGED.add(row.source_ref)
            logger.error(
                "marketplace bundle has no derivable vendor; listing as _unaffiliated",
                extra={"app_id": row.source_ref, "has_source_url": bool(row.source_url)},
            )
        return None, None
    view = author_view(row.author)
    handle = view["github_handle"] if view else None
    name = handle if handle and handle.lower() == key else key
    return key, name


def vendor_repo_error(vendor: str | None, repo: str | None) -> str | None:
    """Validation message for malformed `vendor`/`repo` filters, else `None` (caller sends 400)."""
    if vendor and not _VENDOR_RE.match(vendor.lower()):
        return "vendor must be a GitHub org key or '_unaffiliated'"
    if repo and not _REPO_RE.match(repo.lower()):
        return "repo must look like 'org/repo'"
    return None


def _vendor_repo_filters(query: Any, catalog: Any, vendor: str | None, repo: str | None) -> Any:
    """AND the `vendor` / `repo` filters onto `query`; malformed values raise 400 (no fallback)."""
    if vendor:
        vendor = vendor.lower()
        if not _VENDOR_RE.match(vendor):
            raise bad_request("vendor must be a GitHub org key or '_unaffiliated'")
        if vendor == UNAFFILIATED_VENDOR:
            query &= catalog.vendor_key == None  # noqa: E711
        else:
            query &= catalog.vendor_key == vendor
    if repo:
        repo = repo.lower()
        if not _REPO_RE.match(repo):
            raise bad_request("repo must look like 'org/repo'")
        query &= catalog.source_repo == repo
    return query


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

    vendor_key, vendor_name = _vendor_of(row)
    return CatalogEntry(
        source=row.source,
        sourceId=row.source_id,
        sourceRef=row.source_ref,
        vendorKey=vendor_key,
        vendorName=vendor_name,
        sourceRepo=row.source_repo,
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
        author=to_author_dto(row.author),
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
    vendor: str | None = None,
    repo: str | None = None,
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
    query = _vendor_repo_filters(query, catalog, vendor, repo)

    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=(~catalog.is_core, ~catalog.install_count, ~catalog.created_at, catalog.name),
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


def get_catalog_bundle_entry(
    dal: Any, request: Any, app_id: str, community_id: int | None
) -> CatalogEntry | None:
    """Single `bundle` catalog entry by `app_id` (`source_ref`), tenant-scoped."""
    catalog = dal.marketplace_catalog
    query = _tenant_scope_query(dal, visible_tenant_ids(dal, request))
    query &= (catalog.source == "bundle") & (catalog.source_ref == app_id)
    row = dal(query).select(limitby=(0, 1)).first()
    if row is None:
        return None
    return _to_catalog_entry(row, {}, {})


def get_vendors(dal: Any, request: Any) -> VendorsResponse:
    """Vendor marketplaces (GitHub orgs) with per-repo entry counts, tenant-scoped.

    Same visibility scope as `get_catalog`, so the vendor counts always sum to the unfiltered
    catalog total. Rows with no derivable vendor are reported once, as `_unaffiliated`.
    """
    catalog = dal.marketplace_catalog
    query = _tenant_scope_query(dal, visible_tenant_ids(dal, request))
    rows = dal(query).select(
        catalog.vendor_key,
        catalog.source_repo,
        catalog.source.count().with_alias("n"),
        catalog.author.min().with_alias("sample_author"),
        groupby=catalog.vendor_key | catalog.source_repo,
    )
    by_vendor: dict[str, dict[str, Any]] = {}
    total = 0
    for r in rows:
        key = r.marketplace_catalog.vendor_key or UNAFFILIATED_VENDOR
        repo = r.marketplace_catalog.source_repo
        count = int(r._extra["n"])
        total += count
        bucket = by_vendor.setdefault(key, {"name": None, "count": 0, "repos": []})
        bucket["count"] += count
        bucket["repos"].append(VendorRepoEntry(repo=repo, count=count))
        if bucket["name"] is None and key != UNAFFILIATED_VENDOR:
            view = author_view(r._extra["sample_author"])
            handle = view["github_handle"] if view else None
            bucket["name"] = handle if handle and handle.lower() == key else key
    vendors = [
        VendorEntry(
            key=key,
            name=b["name"] or "Unaffiliated",
            count=b["count"],
            repos=sorted(b["repos"], key=lambda e: (-e.count, e.repo or "")),
        )
        for key, b in by_vendor.items()
    ]
    vendors.sort(key=lambda v: (v.key == UNAFFILIATED_VENDOR, -v.count, v.key))
    return VendorsResponse(success=True, vendors=vendors, total=total)


def get_categories(dal: Any) -> list[CategoryEntry]:
    """Distinct categories + counts, whole-catalog, unfiltered by tenant -- port of `getCategories`.

    Node's own `getCategories()` queries the raw view with no tenant
    filter either -- category NAMES are not tenant-sensitive data (unlike
    module identities/pricing), so this one endpoint is intentionally not
    tenant-scoped, matching the faithful port.
    """
    catalog = dal.marketplace_catalog
    # `marketplace_catalog` has no `id` field (composite `primarykey=`, see
    # `bind_marketplace_module_tables`'s own docstring) -- `source`
    # (never NULL -- `source_id` is NULL for bundle rows) stands in as the COUNT() target.
    # Sorted in Python
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
        catalog.source.count().with_alias("module_count"),
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
