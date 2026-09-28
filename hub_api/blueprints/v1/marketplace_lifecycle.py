"""v1 `marketplace_lifecycle` group -- the App Bundle 3-tier install/available/activate API.

Wires the C3 tables (`app_catalog` / `app_tenant_availability` /
`app_activations`, `config/postgres/migrations/069_app_bundle_tiers.sql`,
`070_app_bundle_catalog_name.sql`) into REST endpoints at three
authorization tiers, narrowing global -> tenant -> community, matching
`docs/plans/2026-08-31-app-bundle-sdk-design.md` Sec5.1's subset invariant
`activated <= available <= installed`:

Three tiers (mount paths relative to this blueprint's `/api/v1/marketplace`
prefix):

- Global (`app_catalog`): install / uninstall, `require_scope
  ("platform:admin")`, mounted at `/bundles`.
- Tenant (`app_tenant_availability`): make/un-available, `require_scope
  ("tenant:admin")` + `require_matching_tenant`, mounted at
  `/tenant/<tenant_slug>/bundles`.
- Community (`app_activations`): activate / deactivate,
  `community_authz.authorize_community(..., admin=True)`, mounted at
  `/community/<community_id>/bundles`.

GET listings at each tier require only the tier's read-level auth (no
elevated scope needed to browse): platform-catalog and tenant-availability
listings need only `tenant_middleware` (any authenticated tenant member --
a community admin deciding what to activate must be able to see what's
available without also holding `tenant:admin`); the community-activation
listing and the resolved-set endpoint require community MEMBERSHIP
(`authorize_community(..., admin=False)`) since `community_id` is otherwise
an IDOR vector across tenants (closed by `authorize_community`'s own
tenant-ownership re-check, see `services/community_authz.py`).

`GET .../community/<community_id>/resolved` surfaces the actual
`flask_core.app_binding.resolve_apps` coexistence set for that community
(community -> tenant -> default ladder) plus any `detect_conflict` hits,
via `services/marketplace_lifecycle_service.py::resolve_community_bundles`.

Response DTOs are deliberately FLAT (no nested single-dataclass field) on
every mutation route so `@validate_response` is safe under an
`insert_async`/`update_async` write without hitting the crash class
`services/dto_response.py` documents (Gotcha #3, `hub_api/PORTING.md`) --
`jsonify_dto()` is not needed anywhere in this group. Request DTOs are
unaffected by that crash (a different code path) and may nest freely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import marketplace_lifecycle_service as svc
from services.community_authz import authorize_community
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

marketplace_lifecycle_bp = Blueprint(
    "v1_marketplace_lifecycle", __name__, url_prefix="/api/v1/marketplace"
)


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- same accessor shape as every other group."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _install_dal() -> AsyncDB | None:
    """The penguin-dal `AsyncDB` handle -- feeds `_guard_*_reconsent()`'s permission-grant reads.

    `None` (not a `KeyError`) when unset -- a handful of this group's own
    test fixtures build a narrower Quart app that never populates
    `install_dal` (this blueprint's pre-existing tables never needed it
    before the permission-reconsent gate); `services.marketplace_
    lifecycle_service`'s own `install_dal: AsyncDB | None = None` default
    treats that the same as an explicit opt-out (its docstring covers
    why: the reconsent gate is additive, not a hard dependency for the
    older, non-permission-catalog `AppManifest` pipeline).
    """
    return cast("AsyncDB | None", current_app.config.get("install_dal"))


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _page_limit() -> tuple[int, int]:
    page = int(request.args.get("page", "1"))
    limit = int(request.args.get("limit", "25"))
    return max(1, page), min(100, max(1, limit))


def _tenant_id(tenant_slug: str) -> int:
    """Validate the URL's `tenant_slug` against the caller's own `TenantContext`, return its id.

    Same one-line precondition `blueprints/v1/tenant.py::_tenant_id` uses --
    reuses `services.tenant_service.require_matching_tenant` directly rather
    than re-deriving the same IDOR-closing check a second time.
    """
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this on the success path
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names (see hub_api/PORTING.md's DTO casing note).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for message-only endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class PlatformCompatibilityInput:
    """Request-side platform-compatibility block (`AppManifest.platform_compatibility`)."""

    testedWith: str = ""
    minVersion: str | None = None
    maxVersion: str | None = None


@dataclass(slots=True, frozen=True)
class InstallBundleRequest:
    """Request DTO for `POST /bundles` (install) -- shaped like `AppManifest`, camelCase wire."""

    appId: str
    name: str
    version: str
    feature: str
    module: str
    provider: str
    executionModel: str = "native"
    isDefault: bool = False
    compatibleWith: list[str] = field(default_factory=list)
    incompatibleWith: list[str] = field(default_factory=list)
    platformCompatibility: PlatformCompatibilityInput = field(
        default_factory=PlatformCompatibilityInput
    )
    author: str | None = None
    license: str | None = None
    sourceUrl: str | None = None
    alternativeTo: list[str] = field(default_factory=list)
    homepageUrl: str | None = None
    notice: str | None = None
    category: str | None = None


@dataclass(slots=True, frozen=True)
class BundleDTO:
    """Response DTO: one `app_catalog` row."""

    appId: str
    name: str | None
    version: str
    module: str
    feature: str
    provider: str
    executionModel: str
    isDefault: bool
    compatibleWith: list[str]
    incompatibleWith: list[str]
    status: str
    installedAt: str | None
    author: str | None
    license: str | None
    licenseReviewRequired: bool
    sourceUrl: str | None
    alternativeTo: list[str]
    homepageUrl: str | None
    notice: str | None
    category: str | None


@dataclass(slots=True, frozen=True)
class BundleListResponse:
    """Response DTO for `GET /bundles` (installed listing)."""

    success: bool
    bundles: list[BundleDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class MakeAvailableRequest:
    """Request DTO for `POST /tenant/<tenant_slug>/bundles` (make-available)."""

    appId: str
    configDefaults: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class AvailabilityDTO:
    """Response DTO: one `app_tenant_availability` row, joined to its `app_catalog` metadata."""

    id: int
    appId: str
    tenantId: int
    available: bool
    configDefaults: dict[str, Any]
    module: str
    feature: str
    name: str | None


@dataclass(slots=True, frozen=True)
class AvailabilityListResponse:
    """Response DTO for `GET /tenant/<tenant_slug>/bundles` (available-to-tenant listing)."""

    success: bool
    bundles: list[AvailabilityDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class ActivateBundleRequest:
    """Request DTO for `POST /community/<community_id>/bundles` (activate)."""

    appId: str
    config: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class ActivationDTO:
    """Response DTO: one `app_activations` row, joined to its `app_catalog` metadata."""

    id: int
    appId: str
    communityId: int
    tenantId: int
    enabled: bool
    config: dict[str, Any]
    module: str
    feature: str
    name: str | None
    activatedAt: str | None


@dataclass(slots=True, frozen=True)
class ActivationListResponse:
    """Response DTO for `GET /community/<community_id>/bundles` (activated-for-community)."""

    success: bool
    bundles: list[ActivationDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class ResolvedAppDTO:
    """Response DTO: one resolved App in a community's active coexistence set."""

    appId: str
    name: str
    version: str
    feature: str
    module: str
    isDefault: bool
    compatibleWith: list[str]
    incompatibleWith: list[str]


@dataclass(slots=True, frozen=True)
class ConflictDTO:
    """Response DTO: one detected coexistence conflict in a resolved set."""

    appId: str
    conflictsWithAppId: str


@dataclass(slots=True, frozen=True)
class ResolvedBundlesResponse:
    """Response DTO for `GET /community/<community_id>/resolved`."""

    success: bool
    apps: list[ResolvedAppDTO]
    conflicts: list[ConflictDTO]


def _manifest_payload(data: InstallBundleRequest) -> dict[str, Any]:
    return {
        "app_id": data.appId,
        "name": data.name,
        "version": data.version,
        "feature": data.feature,
        "module": data.module,
        "provider": data.provider,
        "execution_model": data.executionModel,
        "is_default": data.isDefault,
        "compatible_with": list(data.compatibleWith),
        "incompatible_with": list(data.incompatibleWith),
        "platform_compatibility": {
            "tested_with": data.platformCompatibility.testedWith,
            "min_version": data.platformCompatibility.minVersion,
            "max_version": data.platformCompatibility.maxVersion,
        },
        "author": data.author,
        "license": data.license,
        "source_url": data.sourceUrl,
        "alternative_to": list(data.alternativeTo),
        "homepage_url": data.homepageUrl,
        "notice": data.notice,
        "category": data.category,
    }


def _bundle_dto(row: Any) -> BundleDTO:
    return BundleDTO(
        appId=row.app_id,
        name=row.name,
        version=row.manifest_version,
        module=row.module,
        feature=row.feature,
        provider=row.provider,
        executionModel=row.execution_model,
        isDefault=bool(row.is_default),
        compatibleWith=list(row.compatible_with or []),
        incompatibleWith=list(row.incompatible_with or []),
        status=row.status,
        installedAt=_iso(row.installed_at),
        author=row.author,
        license=row.license,
        licenseReviewRequired=bool(row.license_review_required),
        sourceUrl=row.source_url,
        alternativeTo=list(row.alternative_to or []),
        homepageUrl=row.homepage_url,
        notice=row.notice,
        category=row.category,
    )


def _availability_dto(row: Any) -> AvailabilityDTO:
    ta, cat = row.app_tenant_availability, row.app_catalog
    return AvailabilityDTO(
        id=ta.id,
        appId=ta.app_id,
        tenantId=ta.tenant_id,
        available=bool(ta.available),
        configDefaults=dict(ta.config_defaults or {}),
        module=cat.module if cat is not None else "",
        feature=cat.feature if cat is not None else "",
        name=cat.name if cat is not None else None,
    )


def _activation_dto(row: Any) -> ActivationDTO:
    act, cat = row.app_activations, row.app_catalog
    return ActivationDTO(
        id=act.id,
        appId=act.app_id,
        communityId=act.community_id,
        tenantId=act.tenant_id,
        enabled=bool(act.enabled),
        config=dict(act.config or {}),
        module=cat.module if cat is not None else "",
        feature=cat.feature if cat is not None else "",
        name=cat.name if cat is not None else None,
        activatedAt=_iso(act.activated_at),
    )


# ---------------------------------------------------------------------------
# GLOBAL tier -- app_catalog
# ---------------------------------------------------------------------------


@marketplace_lifecycle_bp.route("/bundles", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(BundleListResponse)
async def list_bundles() -> BundleListResponse:
    """List installed bundles (global catalog) -- any authenticated tenant member."""
    _, dal = _dal()
    page, limit = _page_limit()
    module = request.args.get("module")
    feat = request.args.get("feature")
    provider = request.args.get("provider")
    status = request.args.get("status")
    category = request.args.get("category")
    rows, total, total_pages = await svc.list_installed(
        dal,
        module=module,
        feature=feat,
        provider=provider,
        status=status,
        category=category,
        page=page,
        limit=limit,
    )
    return BundleListResponse(
        success=True,
        bundles=[_bundle_dto(r) for r in rows],
        pagination=PaginationDTO(page=page, limit=limit, total=total, totalPages=total_pages),
    )


@marketplace_lifecycle_bp.route("/bundles", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(InstallBundleRequest)
@validate_response(MessageResponse, 201)
async def install_bundle(
    data: InstallBundleRequest,
) -> tuple[MessageResponse | dict[str, object], int]:
    """Install a bundle into the global catalog."""
    _, dal = _dal()
    try:
        await svc.install_bundle(dal, manifest_data=_manifest_payload(data))
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"Bundle {data.appId} installed"), 201


@marketplace_lifecycle_bp.route("/bundles/<app_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def uninstall_bundle(app_id: str) -> MessageResponse | tuple[dict[str, object], int]:
    """Uninstall (soft-delete -> `status='yanked'`) a bundle from the global catalog."""
    _, dal = _dal()
    try:
        await svc.uninstall_bundle(dal, app_id=app_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"Bundle {app_id} uninstalled")


# ---------------------------------------------------------------------------
# TENANT tier -- app_tenant_availability
# ---------------------------------------------------------------------------


@marketplace_lifecycle_bp.route("/tenant/<tenant_slug>/bundles", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(AvailabilityListResponse)
async def list_available(
    tenant_slug: str,
) -> AvailabilityListResponse | tuple[dict[str, object], int]:
    """List bundles available to this tenant -- any authenticated member of that tenant."""
    async_dal, dal = _dal()
    page, limit = _page_limit()
    module = request.args.get("module")
    feat = request.args.get("feature")
    available_param = request.args.get("available")
    available = None if available_param is None else available_param.lower() != "false"
    try:
        tenant_id = _tenant_id(tenant_slug)
    except ApiError as exc:
        return _err(exc)
    rows, total, total_pages = await svc.list_available(
        async_dal,
        dal,
        tenant_id=tenant_id,
        module=module,
        feature=feat,
        available=available,
        page=page,
        limit=limit,
    )
    return AvailabilityListResponse(
        success=True,
        bundles=[_availability_dto(r) for r in rows],
        pagination=PaginationDTO(page=page, limit=limit, total=total, totalPages=total_pages),
    )


@marketplace_lifecycle_bp.route("/tenant/<tenant_slug>/bundles", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(MakeAvailableRequest)
@validate_response(MessageResponse, 201)
async def make_available(
    data: MakeAvailableRequest, tenant_slug: str
) -> tuple[MessageResponse | dict[str, object], int]:
    """Make an installed bundle available to this tenant. 409 if not installed (superset)."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.make_available(
            async_dal,
            dal,
            tenant_id=tenant_id,
            app_id=data.appId,
            config_defaults=data.configDefaults,
            install_dal=_install_dal(),
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"Bundle {data.appId} made available"), 201


@marketplace_lifecycle_bp.route("/tenant/<tenant_slug>/bundles/<app_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def make_unavailable(
    tenant_slug: str, app_id: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Make a bundle unavailable to this tenant (soft-disable)."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.make_unavailable(async_dal, dal, tenant_id=tenant_id, app_id=app_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"Bundle {app_id} made unavailable")


# ---------------------------------------------------------------------------
# COMMUNITY tier -- app_activations
# ---------------------------------------------------------------------------


@marketplace_lifecycle_bp.route("/community/<int:community_id>/bundles", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(ActivationListResponse)
async def list_activated(
    community_id: int,
) -> ActivationListResponse | tuple[dict[str, object], int]:
    """List bundles activated for this community -- requires active community membership."""
    async_dal, dal = _dal()
    page, limit = _page_limit()
    module = request.args.get("module")
    feat = request.args.get("feature")
    enabled_param = request.args.get("enabled")
    enabled = None if enabled_param is None else enabled_param.lower() != "false"
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
    except ApiError as exc:
        return _err(exc)
    rows, total, total_pages = await svc.list_activated(
        async_dal,
        dal,
        community_id=community_id,
        module=module,
        feature=feat,
        enabled=enabled,
        page=page,
        limit=limit,
    )
    return ActivationListResponse(
        success=True,
        bundles=[_activation_dto(r) for r in rows],
        pagination=PaginationDTO(page=page, limit=limit, total=total, totalPages=total_pages),
    )


@marketplace_lifecycle_bp.route("/community/<int:community_id>/bundles", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(ActivateBundleRequest)
@validate_response(MessageResponse, 201)
async def activate_bundle(
    data: ActivateBundleRequest, community_id: int
) -> tuple[MessageResponse | dict[str, object], int]:
    """Activate a bundle for this community. Requires community-admin membership.

    409 if the bundle is not available to this community's tenant (superset
    invariant) or if it conflicts (`incompatible_with`) with an
    already-enabled activation for this community.
    """
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        caller_id = get_current_user_id(request)
        await svc.activate_bundle(
            async_dal,
            dal,
            community_id=community_id,
            tenant_id=ctx.tenant_id,
            app_id=data.appId,
            config=data.config,
            activated_by=caller_id,
            install_dal=_install_dal(),
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"Bundle {data.appId} activated"), 201


@marketplace_lifecycle_bp.route(
    "/community/<int:community_id>/bundles/<app_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def deactivate_bundle(
    community_id: int, app_id: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate a bundle for this community (soft-disable). Requires community-admin."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        await svc.deactivate_bundle(async_dal, dal, community_id=community_id, app_id=app_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"Bundle {app_id} deactivated")


# ---------------------------------------------------------------------------
# Resolved active set
# ---------------------------------------------------------------------------


@marketplace_lifecycle_bp.route("/community/<int:community_id>/resolved", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(ResolvedBundlesResponse)
async def get_resolved_bundles(
    community_id: int,
) -> ResolvedBundlesResponse | tuple[dict[str, object], int]:
    """Resolve the full active App coexistence set for this community, with any conflicts.

    Ladder: community activation -> tenant availability -> shipped default
    (`flask_core.app_binding.resolve_apps`), unioned across every Feature
    visible to this community's tenant. Requires active community
    membership (`authorize_community(..., admin=False)`).
    """
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
    except ApiError as exc:
        return _err(exc)
    apps, conflicts = await svc.resolve_community_bundles(
        async_dal, dal, tenant_id=ctx.tenant_id, community_id=community_id
    )
    return ResolvedBundlesResponse(
        success=True,
        apps=[
            ResolvedAppDTO(
                appId=a.app_id,
                name=a.name,
                version=a.version,
                feature=a.feature,
                module=a.module,
                isDefault=a.is_default,
                compatibleWith=list(a.compatible_with),
                incompatibleWith=list(a.incompatible_with),
            )
            for a in apps
        ],
        conflicts=[
            ConflictDTO(appId=c.app_id, conflictsWithAppId=c.conflicts_with_app_id)
            for c in conflicts
        ],
    )


BLUEPRINTS: list[Blueprint] = [marketplace_lifecycle_bp]
