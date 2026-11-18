"""v1 operator routes for per-tenant license POOLS (issue #901), global admin only.

- `GET /api/v1/superadmin/tenants/<tenant_id>/license-pools` -- the stored team-axis pool rows.
- `PUT /api/v1/superadmin/tenants/<tenant_id>/license-pools/<tier_code>` `{limit}` -- the global
  admin sets a tenant's Professional (2) / Enterprise (3) pool BY HAND. Allowed ONLY on a
  hardcoded bypass-domain host (`flask_core.entitlement.is_bypass_domain`, evaluated from the
  request host); anywhere else a licensed pool comes from the license server and an operator
  cannot override it -> 409 `POOL_OPERATOR_SET_NOT_ALLOWED`.

`limit`: `null` = not licensed, `-1` = unlimited, `>= 0` = slots. Tenants are keyed by numeric id
exactly like the neighbouring `/superadmin/tenants/<int:tenant_id>` routes. Scope `tenants:admin`
(the `global:admin` bundle's `*:admin`), same as those routes.
"""

# ruff: noqa: N815  (camelCase wire DTOs; tenant_middleware postcondition assert)

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from pydantic import StrictInt
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import community_tier_service as pools
from services import tenant_license_service as lic
from services.audit_events import (
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
)
from services.audit_http import get_service as get_audit_service
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, not_found
from services.pool_provider import license_server_configured, request_is_bypass_domain
from services.system_tenant_guard import require_system_tenant

logger = logging.getLogger(__name__)

superadmin_license_pools_bp = Blueprint(
    "v1_superadmin_license_pools", __name__, url_prefix="/api/v1/superadmin"
)

_SCOPE = "tenants:admin"


@dataclass(slots=True, frozen=True)
class SetPoolRequest:
    """Request DTO for the operator pool PUT: `limit` is REQUIRED (null = not licensed).

    An omitted key is a 400, never an implicit "wipe the pool".
    """

    limit: StrictInt | None


@dataclass(slots=True, frozen=True)
class PoolDTO:
    """One stored pool row."""

    tierCode: int
    limit: int | None
    source: str
    observedAt: str


@dataclass(slots=True, frozen=True)
class PoolsResponse:
    """Response DTO for the pool GET and PUT."""

    success: bool
    tenantId: int
    pools: list[PoolDTO]
    bypassDomain: bool


_SOURCE_NAMES = {
    pools.POOL_SOURCE_LICENSE_SERVER: "license_server",
    pools.POOL_SOURCE_OFFLINE: "offline",
    pools.POOL_SOURCE_OPERATOR_SET: "operator_set",
}


def _on_bypass_domain() -> bool:
    """True when the (trusted, normalized) request host is a hardcoded bypass domain."""
    return request_is_bypass_domain()


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


async def _tenant_exists(async_dal: Any, dal: Any, tenant_id: int) -> bool:
    """Whether the tenant row exists (so a typo'd id 404s instead of creating orphan rows)."""
    import asyncio

    def _job() -> bool:
        return dal(dal.tenants.id == tenant_id).count() > 0  # type: ignore[no-any-return]

    return bool(await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job))


async def _read(async_dal: Any, dal: Any, tenant_id: int) -> PoolsResponse:
    store = pools.PoolStore(async_dal, dal)
    out: list[PoolDTO] = []
    for tier in pools.POOLED_TIER_CODES:
        row = await store.get(tenant_id, tier)
        if row is not None:
            out.append(
                PoolDTO(
                    tierCode=tier,
                    limit=row.limit_count,
                    source=_SOURCE_NAMES[row.source_code],
                    observedAt=row.observed_at.isoformat(),
                )
            )
    return PoolsResponse(
        success=True, tenantId=tenant_id, pools=out, bypassDomain=_on_bypass_domain()
    )


@superadmin_license_pools_bp.route("/tenants/<int:tenant_id>/license-pools", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SCOPE)  # type: ignore[misc]
@require_system_tenant  # type: ignore[misc]
@validate_response(PoolsResponse)
async def get_license_pools(tenant_id: int) -> PoolsResponse | tuple[dict[str, object], int]:
    """Return the stored pool rows for a tenant."""
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    if not await _tenant_exists(async_dal, dal, tenant_id):
        return _err(not_found("Tenant not found"))
    return await _read(async_dal, dal, tenant_id)


@superadmin_license_pools_bp.route(
    "/tenants/<int:tenant_id>/license-pools/<int:tier_code>", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(_SCOPE)  # type: ignore[misc]
@require_system_tenant  # type: ignore[misc]
@validate_request(SetPoolRequest)
@validate_response(PoolsResponse)
async def put_license_pool(
    data: SetPoolRequest, tenant_id: int, tier_code: int
) -> PoolsResponse | tuple[dict[str, object], int]:
    """Set a tenant's pool by hand (bypass domains only; 409 elsewhere)."""
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    try:
        if tier_code not in pools.POOLED_TIER_CODES:
            raise bad_request("tier_code must be 2 (professional) or 3 (enterprise)")
        limit = lic.require_valid_limit(data.limit)
        if not _on_bypass_domain():
            raise ApiError(
                "Pools are licensed per tenant on this deployment; an operator cannot set them",
                409,
                "POOL_OPERATOR_SET_NOT_ALLOWED",
            )
        if not await _tenant_exists(async_dal, dal, tenant_id):
            raise not_found("Tenant not found")
        # Defense in depth: the bypass decision rests on the request host. If this deployment
        # has a license server configured AND the tenant holds a license key, the pool belongs
        # to the license server, so a (possibly spoofed-host) operator write is refused.
        if (
            license_server_configured()
            and (await lic.get_license_status(async_dal, dal, tenant_id)).configured
        ):
            raise ApiError(
                "This tenant's pool is licensed by the license server; an operator cannot set it",
                409,
                "POOL_OPERATOR_SET_NOT_ALLOWED",
            )
    except ApiError as exc:
        logger.debug(
            "superadmin_license_pools.rejected code=%s status=%s", exc.code, exc.status_code
        )
        return _err(exc)
    actor = get_current_user_id(request)
    await pools.PoolStore(async_dal, dal).put(
        tenant_id, tier_code, limit, pools.POOL_SOURCE_OPERATOR_SET, pools.now_utc(), actor
    )
    audit = get_audit_service()
    if audit is not None:
        await pools._record(  # noqa: SLF001 - retry-once-then-loud audit helper
            audit,
            AuditEvent(
                category=AuditCategory.LICENSE,
                action=AuditAction.LICENSE_TENANT_POOL_SET.value,
                outcome=AuditOutcome.SUCCESS,
                actor_kind=ActorKind.USER,
                actor_user_id=actor,
                tenant_id=tenant_id,
                target_type="tenant_license_pool",
                target_id=str(tenant_id),
                details={"tier_code": tier_code, "limit": limit},
            ),
        )
    return await _read(async_dal, dal, tenant_id)


BLUEPRINTS: list[Blueprint] = [superadmin_license_pools_bp]
