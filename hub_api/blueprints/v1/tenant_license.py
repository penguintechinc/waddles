"""v1 `tenant_license` group -- a tenant admin's own license key (issue #901).

Mount: `/api/v1/tenant/<tenant_slug>/license`. `tenant_middleware` (outermost; the URL slug must
equal the JWT's tenant, mismatch = 403) then `require_scope("tenant:admin")`.

- `GET    /license` -- status only: `{configured, fingerprint, setAt}`.
- `PUT    /license` -- write-only `{licenseKey}`: seals and stores it; the response never echoes it.
- `DELETE /license` -- clears it (the tenant becomes a Free tenant: its pool resolves to
  "not licensed", `limit: null`).

The key is a credential: it is never returned, logged or audited (only its fingerprint is).

These key routes are deliberately NOT feature-flag gated (a tenant admin must always be able to
set, rotate or revoke their own credential); the community-tier allocation endpoints keep their
own flag. Setting or clearing a key evicts that tenant's cached license-server pools.
"""

# ruff: noqa: N815, S101  (camelCase wire DTOs; tenant_middleware postcondition assert)

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import tenant_license_service as svc
from services.audit_http import get_service as get_audit_service
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

logger = logging.getLogger(__name__)

tenant_license_bp = Blueprint("v1_tenant_license", __name__, url_prefix="/api/v1/tenant")

#: Held by every tenant admin (`tenant:admin` bundle).
LICENSE_ADMIN_SCOPE = "tenant:admin"


@dataclass(slots=True, frozen=True)
class SetLicenseKeyRequest:
    """Request DTO for `PUT /license`; the key is validated in the service, never echoed."""

    licenseKey: str = field(repr=False)


@dataclass(slots=True, frozen=True)
class LicenseStatusDTO:
    """The only license-key facts that leave the server."""

    configured: bool
    fingerprint: str | None
    setAt: str | None


@dataclass(slots=True, frozen=True)
class LicenseStatusResponse:
    """Response DTO for GET/PUT."""

    success: bool
    license: LicenseStatusDTO


@dataclass(slots=True, frozen=True)
class LicenseClearedResponse:
    """Response DTO for DELETE."""

    success: bool
    cleared: bool


def _dals() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


def _tenant(tenant_slug: str) -> int:
    """Validate the URL slug against the caller's own tenant context; return the tenant id."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this on the success path
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


def _dto(status: svc.LicenseStatus) -> LicenseStatusDTO:
    return LicenseStatusDTO(
        configured=status.configured,
        fingerprint=status.fingerprint,
        setAt=status.set_at.isoformat() if status.set_at else None,
    )


@tenant_license_bp.route("/<tenant_slug>/license", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(LICENSE_ADMIN_SCOPE)  # type: ignore[misc]
@validate_response(LicenseStatusResponse)
async def get_license(tenant_slug: str) -> LicenseStatusResponse | tuple[dict[str, object], int]:
    """Return whether a key is configured, its fingerprint and when it was set."""
    try:
        tenant_id = _tenant(tenant_slug)
        async_dal, dal = _dals()
        status = await svc.get_license_status(async_dal, dal, tenant_id)
    except ApiError as exc:
        logger.debug("tenant_license.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return LicenseStatusResponse(success=True, license=_dto(status))


@tenant_license_bp.route("/<tenant_slug>/license", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(LICENSE_ADMIN_SCOPE)  # type: ignore[misc]
@validate_request(SetLicenseKeyRequest)
@validate_response(LicenseStatusResponse)
async def put_license(
    data: SetLicenseKeyRequest, tenant_slug: str
) -> LicenseStatusResponse | tuple[dict[str, object], int]:
    """Seal and store the tenant's license key (write-only; never echoed)."""
    try:
        tenant_id = _tenant(tenant_slug)
        async_dal, dal = _dals()
        status = await svc.set_tenant_license_key(
            async_dal,
            dal,
            tenant_id=tenant_id,
            license_key=data.licenseKey,
            actor=get_current_user_id(request),
            audit=get_audit_service(),
        )
    except ApiError as exc:
        logger.debug("tenant_license.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return LicenseStatusResponse(success=True, license=_dto(status))


@tenant_license_bp.route("/<tenant_slug>/license", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(LICENSE_ADMIN_SCOPE)  # type: ignore[misc]
@validate_response(LicenseClearedResponse)
async def delete_license(
    tenant_slug: str,
) -> LicenseClearedResponse | tuple[dict[str, object], int]:
    """Clear the tenant's license key."""
    try:
        tenant_id = _tenant(tenant_slug)
        async_dal, dal = _dals()
        cleared = await svc.clear_tenant_license_key(
            async_dal,
            dal,
            tenant_id=tenant_id,
            actor=get_current_user_id(request),
            audit=get_audit_service(),
        )
    except ApiError as exc:
        logger.debug("tenant_license.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return LicenseClearedResponse(success=True, cleared=cleared)


BLUEPRINTS: list[Blueprint] = [tenant_license_bp]
