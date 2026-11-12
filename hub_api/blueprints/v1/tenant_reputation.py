"""v1 tenant-level reputation policy -- `GET|PUT /api/v1/tenant/<tenant_slug>/reputation/config`.

TENANT admins only (`require_scope("tenant:admin")` + `require_matching_tenant`: the URL slug must
equal the caller's validated JWT tenant -- a global admin reaches another tenant by assuming it,
never by passing a different slug). A COMMUNITY admin lacks `tenant:admin` and gets 403.

The tenant policy is a single floor compared against the user's tenant aggregate score; when
enabled (default OFF) the accrual bundle bans the user across every community of the tenant
(flag `waddles.reputation-auto-ban`). Only floors/thresholds live here -- scoring weights are the
reputation logic bundle's own config. There is deliberately no platform/global ban.

Discovery contract: module-level `BLUEPRINTS` (auto-mounted by `routers/_discovery.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import document_response, validate_request

from services import community_reputation_service as reputation_svc
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

tenant_reputation_bp = Blueprint("v1_tenant_reputation", __name__, url_prefix="/api/v1/tenant")

SCOPE_TENANT_ADMIN = "tenant:admin"


@dataclass(slots=True, frozen=True)
class TenantReputationConfigRequest:
    """PUT body -- only supplied fields change; 300 <= floor < atRiskThreshold <= 850."""

    tenantAutoBanEnabled: bool | None = None
    tenantAutoBanFloor: int | None = None
    tenantAtRiskThreshold: int | None = None


@dataclass(slots=True, frozen=True)
class TenantReputationConfigResponse:
    """Tenant reputation policy."""

    success: bool
    tenantAutoBanEnabled: bool
    tenantAutoBanFloor: int
    tenantAtRiskThreshold: int
    updatedAt: str | None


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _dto(cfg: dict[str, Any]) -> TenantReputationConfigResponse:
    return TenantReputationConfigResponse(
        success=True,
        tenantAutoBanEnabled=cfg["tenant_auto_ban_enabled"],
        tenantAutoBanFloor=cfg["tenant_auto_ban_floor"],
        tenantAtRiskThreshold=cfg["tenant_at_risk_threshold"],
        updatedAt=cfg["updated_at"],
    )


@tenant_reputation_bp.route("/<tenant_slug>/reputation/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@document_response(TenantReputationConfigResponse)
async def get_tenant_reputation_config(tenant_slug: str) -> tuple[Any, int]:
    """The caller's own tenant's reputation policy."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        require_matching_tenant(tenant_slug, ctx.tenant_slug)
        cfg = await reputation_svc.get_tenant_config(
            current_app.config["async_dal"], current_app.config["dal"], tenant_id=ctx.tenant_id
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(_dto(cfg))


@tenant_reputation_bp.route("/<tenant_slug>/reputation/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[misc]
@validate_request(TenantReputationConfigRequest)
@document_response(TenantReputationConfigResponse)
async def put_tenant_reputation_config(
    data: TenantReputationConfigRequest, tenant_slug: str
) -> tuple[Any, int]:
    """Set the tenant auto-ban toggle / floor / at-risk threshold (400 if out of range)."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        require_matching_tenant(tenant_slug, ctx.tenant_slug)
        cfg = await reputation_svc.set_tenant_config(
            current_app.config["async_dal"],
            current_app.config["dal"],
            tenant_id=ctx.tenant_id,
            auto_ban_enabled=data.tenantAutoBanEnabled,
            auto_ban_floor=data.tenantAutoBanFloor,
            at_risk_threshold=data.tenantAtRiskThreshold,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(_dto(cfg))


BLUEPRINTS: list[Blueprint] = [tenant_reputation_bp]
