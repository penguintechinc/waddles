# ruff: noqa: N815  # camelCase wire DTOs: the /api/v1 JSON contract
"""v1 `tenant_community_tiers` group -- tenant-admin allocation of Pro/Ent slots to communities.

Mount: `/api/v1/tenant/<tenant_slug>/...`. Every route is `tenant_middleware` (outermost),
then `require_scope("tenant:admin", <per-action scope>)`, then the URL slug must equal the JWT's
tenant, then the `waddles.community-tier-allocation` rollout flag (OFF = uniform 404, so the
feature's existence never leaks). The flag gates ONLY this tenant-admin API; the superadmin
override (`PUT /api/v1/superadmin/communities/<id>`) is never flag-gated.

- `GET /license/pool` -- the tenant's team-axis pool per tier (`limit` null = not licensed,
  -1 = unlimited), with when it was observed and whether it is stale.
- `GET /communities/tier-allocations` -- active Pro/Ent allocations (Free has no row), paginated,
  filterable by `tier` and `source`; invalid filter values are 400.
- `PUT /communities/<community_uuid>/tier` -- set a community's tier (1 Free releases its slot,
  2 Professional, 3 Enterprise) via `community_tier_service.allocate(source=tenant_admin)`.

Errors: 402 `TEAM_POOL_EXHAUSTED` (`requiredTier`, `limit`, `used`, `upgrade`), 503
`TEAM_POOL_UNAVAILABLE` (the pool size could not be determined -- never a silent zero), 404 for an
unknown / other-tenant / malformed community UUID and when the flag is OFF. Enum fields are int
codes; only `public_id` UUIDs cross the boundary (the numeric community id never does). Every
response is an explicit DTO.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Final, cast

from flask_core.api_utils import error_response
from flask_core.auth import SCOPE_TENANT_COMMUNITY_TIER_ALLOCATE, SCOPE_TENANT_COMMUNITY_TIER_READ
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import ConfigDict, Field
from quart import Blueprint, current_app, request
from quart_schema import RequestSchemaValidationError, validate_request, validate_response

from services import community_tier_service as svc
from services.audit_http import get_service as get_audit_service
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, not_found
from services.pool_provider import get_pool_provider
from services.schema import bind_community_tier_tables
from services.tenant_service import require_matching_tenant

logger = logging.getLogger(__name__)

tenant_community_tiers_bp = Blueprint(
    "v1_tenant_community_tiers", __name__, url_prefix="/api/v1/tenant"
)

#: PostHog flag (default OFF) gating this tenant-admin API only.
FEATURE_COMMUNITY_TIER_ALLOCATION: Final[str] = "waddles.community-tier-allocation"

_DEFAULT_LIMIT: Final[int] = 50
_MAX_LIMIT: Final[int] = 200
_VALID_TIERS: Final[frozenset[int]] = frozenset(svc.TIER_NAMES)
_VALID_SOURCES: Final[frozenset[int]] = frozenset(
    {svc.SOURCE_TENANT_ADMIN, svc.SOURCE_OPERATOR_OVERRIDE, svc.SOURCE_BACKFILL}
)


@dataclass(slots=True, frozen=True)
class PoolTierDTO:
    """One tier's pool. `limit`: null = not licensed, -1 = unlimited. `tier`: 2 Pro / 3 Ent."""

    tier: int
    limit: int | None
    used: int
    available: int | None
    overage: int


@dataclass(slots=True, frozen=True)
class PoolResponse:
    """Response DTO for `GET /license/pool`."""

    success: bool
    tiers: list[PoolTierDTO]
    observedAt: str | None
    stale: bool
    source: str | None


@dataclass(slots=True, frozen=True)
class AllocationDTO:
    """One active allocation. `communityId` is the community's `public_id` UUID."""

    communityId: str
    tier: int
    source: int
    allocatedAt: str
    stale: bool


@dataclass(slots=True, frozen=True)
class AllocationMetaDTO:
    """Pagination metadata for the allocation list."""

    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class AllocationListResponse:
    """Response DTO for `GET /communities/tier-allocations`."""

    success: bool
    allocations: list[AllocationDTO] = field(default_factory=list)
    meta: AllocationMetaDTO = field(default_factory=lambda: AllocationMetaDTO(0, 0, 0))


@dataclass(slots=True, frozen=True)
class SetTierRequest:
    """Request DTO for `PUT ./tier`: `tier` is 1 Free, 2 Professional or 3 Enterprise."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    tier: Annotated[int, Field(strict=True)]


@dataclass(slots=True, frozen=True)
class SetTierResponse:
    """Response DTO for `PUT /communities/<uuid>/tier` (`limit` null = not licensed / Free)."""

    success: bool
    communityId: str
    tier: int
    previousTier: int
    changed: bool
    used: int
    limit: int | None
    overage: int


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


@tenant_community_tiers_bp.errorhandler(RequestSchemaValidationError)  # type: ignore[misc]
async def _request_validation_error(exc: RequestSchemaValidationError) -> Any:
    """JSON error envelope naming each invalid field (never a bare empty 400)."""
    pydantic_err = getattr(exc, "validation_error", None)
    issues = pydantic_err.errors() if pydantic_err is not None else []
    parts = [
        f"{'.'.join(str(p) for p in i.get('loc', ())) or 'body'}: {i.get('msg', 'invalid')}"
        for i in issues
    ]
    message = "Invalid request: " + "; ".join(parts) if parts else "Invalid request body"
    body, status = error_response(message, 400, "VALIDATION_ERROR")
    return body, status


async def _tenant(tenant_slug: str) -> tuple[int, str]:
    """Slug-match -> rollout flag. Returns `(tenant_id, tenant_slug)`; flag OFF is a 404."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # noqa: S101  # nosec B101 - tenant_middleware postcondition
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    if not await feature_enabled(FEATURE_COMMUNITY_TIER_ALLOCATION, tenant=ctx.tenant_slug):
        raise not_found("Not found")
    return int(ctx.tenant_id), str(ctx.tenant_slug)


def _canonical_uuid(raw: str | None) -> str | None:
    """Canonical lowercase UUID string, or None when malformed."""
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None


async def _resolve_community(tenant_id: int, community_uuid: str) -> int:
    """Internal community id for a public UUID inside `tenant_id`; any miss is the same 404."""
    canonical = _canonical_uuid(community_uuid)
    if canonical is None:
        raise not_found("Community not found")
    async_dal, dal = _dal()

    def _job() -> int | None:
        c = dal.communities
        row = dal((c.public_id == canonical) & (c.tenant_id == tenant_id)).select(c.id).first()
        return None if row is None else int(row.id)

    found = await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)
    if found is None:
        raise not_found("Community not found")
    return found


def _int_param(name: str, valid: frozenset[int]) -> int | None:
    """Strict integer-code query parameter; anything malformed or unknown is a 400."""
    raw = request.args.get(name)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) not in valid:
        raise bad_request(f"{name} must be one of {sorted(valid)}")
    return int(raw)


def _page_params() -> tuple[int, int]:
    """`(limit, offset)` with strict validation (400 on garbage, limit capped at 200)."""
    out: list[int] = []
    for name, default, low, high in (
        ("limit", _DEFAULT_LIMIT, 1, _MAX_LIMIT),
        ("offset", 0, 0, 10_000_000),
    ):
        raw = request.args.get(name)
        if raw is None:
            out.append(default)
            continue
        if not raw.isascii() or not raw.isdigit() or not low <= int(raw) <= high:
            raise bad_request(f"{name} must be an integer between {low} and {high}")
        out.append(int(raw))
    return out[0], out[1]


async def _pool_info_summary(
    tenant_id: int, tiers: tuple[int, ...]
) -> tuple[datetime | None, bool, str | None]:
    """Oldest observation, any-stale flag and the source across the pooled tiers."""
    provider = get_pool_provider(current_app)
    infos = [await provider.get_pool(tenant_id, t) for t in tiers]
    return (
        min(i.observed_at for i in infos),
        any(i.stale for i in infos),
        infos[0].source,
    )


@tenant_community_tiers_bp.route("/<tenant_slug>/license/pool", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenant:admin", SCOPE_TENANT_COMMUNITY_TIER_READ)  # type: ignore[misc]
@validate_response(PoolResponse)
async def get_pool(tenant_slug: str) -> PoolResponse | tuple[dict[str, object], int]:
    """The tenant's team-axis pool per tier (limit / used / available / overage)."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _tenant(tenant_slug)
        provider = get_pool_provider(current_app)
        usage = await svc.pool_usage(async_dal, dal, tenant_id, pool=provider)
        observed, stale, source = await _pool_info_summary(tenant_id, svc.POOLED_TIER_CODES)
    except ApiError as exc:
        logger.debug("tenant_community_tiers.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    except svc.PoolUnavailable:
        logger.warning("tenant_community_tiers.pool_unavailable")
        return _pool_unavailable()
    return PoolResponse(
        success=True,
        tiers=[
            PoolTierDTO(
                tier=tier,
                limit=u.limit,
                used=u.used,
                available=u.available,
                overage=u.overage,
            )
            for tier, u in sorted(usage.items())
        ],
        observedAt=observed.isoformat() if observed else None,
        stale=stale,
        source=source,
    )


@tenant_community_tiers_bp.route("/<tenant_slug>/communities/tier-allocations", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenant:admin", SCOPE_TENANT_COMMUNITY_TIER_READ)  # type: ignore[misc]
@validate_response(AllocationListResponse)
async def list_allocations(
    tenant_slug: str,
) -> AllocationListResponse | tuple[dict[str, object], int]:
    """Active Pro/Ent allocations of the tenant's communities (paginated; `tier`/`source`)."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _tenant(tenant_slug)
        tier = _int_param("tier", _VALID_TIERS)
        source = _int_param("source", _VALID_SOURCES)
        limit, offset = _page_params()
    except ApiError as exc:
        logger.debug("tenant_community_tiers.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    bind_community_tier_tables(dal)
    now = svc.now_utc()

    def _job() -> tuple[int, list[tuple[str, Any]]]:
        t, c = dal.community_tier_allocations, dal.communities
        query = (t.tenant_id == tenant_id) & (t.released_at == None)  # noqa: E711
        if tier is not None:
            query &= t.tier_code == tier
        if source is not None:
            query &= t.source_code == source
        total = int(dal(query).count())
        rows = dal(query).select(
            t.ALL, orderby=~t.allocated_at | t.id, limitby=(offset, offset + limit)
        )
        public_ids = {
            int(r.id): str(r.public_id)
            for r in dal(c.id.belongs([int(r.community_id) for r in rows])).select(
                c.id, c.public_id
            )
        }
        return total, [(public_ids.get(int(r.community_id), ""), r) for r in rows]

    total, rows = await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)
    return AllocationListResponse(
        success=True,
        allocations=[
            AllocationDTO(
                communityId=pid,
                tier=int(r.tier_code),
                source=int(r.source_code),
                allocatedAt=_iso(svc._aware(r.allocated_at)),  # noqa: SLF001
                stale=svc._is_stale(r, now),  # noqa: SLF001
            )
            for pid, r in rows
            if pid
        ],
        meta=AllocationMetaDTO(total=total, limit=limit, offset=offset),
    )


def _pool_unavailable() -> tuple[dict[str, object], int]:
    """503 fail-loud: the pool limit could not be determined (never a silent zero)."""
    return cast(
        tuple[dict[str, object], int],
        error_response(
            "The team pool for this tenant could not be determined",
            503,
            "TEAM_POOL_UNAVAILABLE",
        ),
    )


@tenant_community_tiers_bp.route(
    "/<tenant_slug>/communities/<community_uuid>/tier", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("tenant:admin", SCOPE_TENANT_COMMUNITY_TIER_ALLOCATE)  # type: ignore[misc]
@validate_request(SetTierRequest)
@validate_response(SetTierResponse)
async def set_community_tier(
    data: SetTierRequest, tenant_slug: str, community_uuid: str
) -> SetTierResponse | tuple[dict[str, object], int]:
    """Allocate (or release, tier 1) a community's tier from the tenant's team pool."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _tenant(tenant_slug)
        if data.tier not in _VALID_TIERS:
            raise bad_request(f"tier must be one of {sorted(_VALID_TIERS)}")
        community_id = await _resolve_community(tenant_id, community_uuid)
        result = await svc.allocate(
            async_dal,
            dal,
            tenant_id=tenant_id,
            community_id=community_id,
            tier=data.tier,
            actor=get_current_user_id(request),
            source=svc.SOURCE_TENANT_ADMIN,
            pool=get_pool_provider(current_app),
            audit=get_audit_service(),
        )
    except ApiError as exc:
        logger.debug("tenant_community_tiers.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    except svc.CommunityNotInTenant:
        logger.debug("tenant_community_tiers.community_not_in_tenant")
        return _err(not_found("Community not found"))
    except svc.PoolExhausted as exc:
        logger.info("tenant_community_tiers.pool_exhausted required_tier=%s", exc.required_tier)
        return cast(
            tuple[dict[str, object], int],
            error_response(
                f"No free {svc.TIER_NAMES[exc.required_tier]} slot in this tenant's team pool",
                402,
                "TEAM_POOL_EXHAUSTED",
                # No upgrade/pricing URL is configured anywhere in hub-api yet, so only the
                # `upgrade: true` marker (same convention as free_tier_caps) is sent.
                {
                    "requiredTier": exc.required_tier,
                    "limit": exc.limit,
                    "used": exc.used,
                    "upgrade": True,
                },
            ),
        )
    except svc.PoolUnavailable:
        logger.warning("tenant_community_tiers.pool_unavailable")
        return _pool_unavailable()
    return SetTierResponse(
        success=True,
        communityId=str(_canonical_uuid(community_uuid)),
        tier=result.tier_code,
        previousTier=result.previous_tier_code,
        changed=result.changed,
        used=result.used,
        limit=result.limit,
        overage=result.overage,
    )


BLUEPRINTS: list[Blueprint] = [tenant_community_tiers_bp]
