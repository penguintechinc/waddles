"""v1 `tenant_kms` group -- Enterprise external KMS / BYOK for a tenant's data keys.

Mount: `/api/v1/tenant/<tenant_slug>/kms`. Every route is
`tenant_middleware` (outermost; the URL slug must equal the JWT's tenant) then
`require_scope("compliance.kms:admin")` -- the scope the
`compliance.external_kms` Feature contract declares. Role names are never
consulted.

- `GET    /kms` -- no entitlement: config + no-secret key summary (a lapsed
  licence must still be able to see its state).
- `PUT    /kms` -- Enterprise: record the customer key as `pending`; returns
  the ExternalId (the customer's proof-of-control token).
- `POST   /kms/activate` -- Enterprise: verify the key, re-wrap every DEK onto
  it, flip the config to `active`.
- `DELETE /kms` -- no entitlement: exit ramp, re-wrap every DEK back to the
  platform baseline.

The baseline needs none of this -- a tenant that never calls these routes is
on the platform-managed key, forever, with zero configuration. Responses are
explicit DTOs (`security.md` Output Validation): wrapped DEK bytes, KEK
material and provider error text never leave the process; only counts,
versions and identifiers do.

Failures are loud and typed (see `_envelope_error`): a revoked or unreachable
customer key is a 4xx/5xx with a stable `code`, never a silent fallback to the
platform key.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from pydantic import Field
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services.current_user import get_current_user_id
from services.envelope import (
    EnvelopeError,
    EnvelopeInputError,
    ExternalKmsNotEntitledError,
    KmsAccessDeniedError,
    KmsConfigError,
    KmsRejectedError,
    KmsUnavailableError,
    PlatformKekError,
    RewrapReport,
    TenantKeyUnavailableError,
    TenantKmsConfig,
    TenantKmsStatus,
)
from services.envelope.models import DekRecord
from services.envelope.runtime import EnvelopeRuntime
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

logger = logging.getLogger(__name__)

tenant_kms_bp = Blueprint("v1_tenant_kms", __name__, url_prefix="/api/v1/tenant")

#: Scope the `compliance.external_kms` Feature contract requires.
KMS_ADMIN_SCOPE = "compliance.kms:admin"


@dataclass(slots=True, frozen=True)
class KmsConfigRequest:
    """Request DTO for `PUT /kms`: where the customer's key lives (no secrets).

    Lengths are capped at the boundary (the request body limit is sized for bundle uploads, not
    for a key identifier); the provider-specific validators then apply strict patterns.
    """

    provider: Annotated[str, Field(min_length=1, max_length=64)]
    keyRef: Annotated[str, Field(min_length=1, max_length=2048)]
    region: Annotated[str, Field(max_length=64)] | None = None
    principal: Annotated[str, Field(max_length=2048)] | None = None


@dataclass(slots=True, frozen=True)
class KmsConfigDTO:
    """Response DTO: one tenant KMS config. `externalId` is the proof-of-control token."""

    provider: str
    keyRef: str
    region: str | None
    principal: str | None
    externalId: str
    status: str
    lastVerifiedAt: str | None
    lastErrorCode: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class DekDTO:
    """Response DTO: a no-secrets summary of one DEK version."""

    dekVersion: int
    kekKind: str
    kekRef: str
    status: str
    usageCount: int
    activatedAt: str | None
    retiredAt: str | None


@dataclass(slots=True, frozen=True)
class RewrapDTO:
    """Response DTO: outcome of re-wrapping every DEK version onto a target KEK."""

    targetKind: str
    targetRef: str
    total: int
    rewrapped: int
    alreadyCurrent: int
    failedVersions: list[int]
    ok: bool


@dataclass(slots=True, frozen=True)
class KmsStatusResponse:
    """Response DTO for `GET /kms`."""

    success: bool
    config: KmsConfigDTO | None
    keys: list[DekDTO]
    supportedProviders: list[str]
    #: Provider id -> the public identity to trust/grant on the customer's side.
    platformPrincipals: dict[str, str]


@dataclass(slots=True, frozen=True)
class KmsConfigResponse:
    """Response DTO for `PUT /kms`."""

    success: bool
    config: KmsConfigDTO


@dataclass(slots=True, frozen=True)
class KmsActivateResponse:
    """Response DTO for `POST /kms/activate`."""

    success: bool
    config: KmsConfigDTO
    rewrap: RewrapDTO


@dataclass(slots=True, frozen=True)
class KmsDisableResponse:
    """Response DTO for `DELETE /kms`."""

    success: bool
    rewrap: RewrapDTO


def _runtime() -> EnvelopeRuntime:
    """The process-wide envelope runtime (built in `app.py`'s `before_serving`)."""
    runtime = current_app.config.get("envelope_runtime")
    if runtime is None:
        raise ApiError("Envelope encryption is not initialised", 503, "ENVELOPE_UNAVAILABLE")
    return cast(EnvelopeRuntime, runtime)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _config_dto(config: TenantKmsConfig) -> KmsConfigDTO:
    return KmsConfigDTO(
        provider=config.provider,
        keyRef=config.key_ref,
        region=config.region,
        principal=config.principal,
        externalId=config.external_id,
        status=config.status,
        lastVerifiedAt=_iso(config.last_verified_at),
        lastErrorCode=config.last_error_code,
        updatedAt=_iso(config.updated_at),
    )


def _dek_dto(record: DekRecord) -> DekDTO:
    return DekDTO(
        dekVersion=record.dek_version,
        kekKind=record.kek_kind,
        kekRef=record.kek_ref,
        status=record.status,
        usageCount=record.usage_count,
        activatedAt=_iso(record.activated_at),
        retiredAt=_iso(record.retired_at),
    )


def _rewrap_dto(report: RewrapReport) -> RewrapDTO:
    return RewrapDTO(
        targetKind=report.target_kind,
        targetRef=report.target_ref,
        total=report.total,
        rewrapped=report.rewrapped,
        alreadyCurrent=report.already_current,
        failedVersions=list(report.failed_versions),
        ok=report.ok,
    )


def _status_response(status: TenantKmsStatus, principals: Mapping[str, str]) -> KmsStatusResponse:
    return KmsStatusResponse(
        success=True,
        config=_config_dto(status.config) if status.config is not None else None,
        keys=[_dek_dto(k) for k in status.keys],
        supportedProviders=list(status.supported_providers),
        platformPrincipals={k: v for k, v in principals.items() if k in status.supported_providers},
    )


def _err(code: str, message: str, status: int) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(message, status, code))


def _envelope_error(tenant_id: int, exc: ApiError | EnvelopeError) -> tuple[dict[str, object], int]:
    """Map an envelope failure to a stable HTTP status + `code`; never echo provider text.

    Order matters: more specific `KmsError` subclasses first. The caller has already logged the
    failure (type only -- messages are fixed strings but the rule is uniform) in its `except`
    body; this function adds the extra ERROR records for the two cases ops must notice.
    """
    if isinstance(exc, ApiError):
        return _err(exc.code, exc.message, exc.status_code)
    if isinstance(exc, ExternalKmsNotEntitledError):
        return _err(
            "EXTERNAL_KMS_NOT_ENTITLED",
            "External KMS requires the Enterprise compliance.external_kms entitlement",
            403,
        )
    if isinstance(exc, KmsConfigError | EnvelopeInputError):
        return _err("INVALID_KMS_CONFIG", str(exc), 422)
    if isinstance(exc, KmsAccessDeniedError):
        return _err(
            "KMS_ACCESS_DENIED",
            "The key provider denied access; check the key grant, trust policy and key state",
            422,
        )
    if isinstance(exc, KmsUnavailableError):
        return _err("KMS_UNAVAILABLE", "The key provider is unreachable; retry later", 503)
    if isinstance(exc, KmsRejectedError):
        return _err("KMS_REJECTED", "The key provider rejected the request", 502)
    if isinstance(exc, TenantKeyUnavailableError):
        return _err("TENANT_KEY_UNAVAILABLE", f"Tenant key unavailable ({exc.reason})", 503)
    if isinstance(exc, PlatformKekError):
        logger.error("envelope.api.platform_kek_unavailable", extra={"tenant_id": tenant_id})
        return _err("PLATFORM_KEK_UNAVAILABLE", "The platform key is not available", 503)
    logger.error("envelope.api.unexpected", extra={"tenant_id": tenant_id}, exc_info=True)
    return _err("ENVELOPE_ERROR", "Envelope encryption failed", 500)


def _tenant(tenant_slug: str) -> tuple[int, str]:
    """Validate the URL slug against the caller's own `TenantContext`; return `(id, slug)`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this on the success path
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id), cast(str, ctx.tenant_slug)


async def _audit(tenant_id: int, action: str, details: dict[str, Any]) -> None:
    """Write an `audit_log` row for a KMS change; a failure is logged loudly, never swallowed."""
    try:
        await _install_dal().audit_log.async_insert(
            user_id=get_current_user_id(request),
            action=action,
            target_type="tenant_kms",
            target_id=str(tenant_id),
            details=details,
            created_at=datetime.now(UTC),
        )
    except Exception:
        logger.error(
            "envelope.api.audit_failed",
            extra={"tenant_id": tenant_id, "action": action},
            exc_info=True,
        )


@tenant_kms_bp.route("/<tenant_slug>/kms", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(KMS_ADMIN_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(KmsStatusResponse)
async def get_kms_status(
    tenant_slug: str,
) -> KmsStatusResponse | tuple[dict[str, object], int]:
    """Return the tenant's KMS config and a no-secrets DEK summary (not entitlement-gated)."""
    tenant_id = 0
    try:
        tenant_id, _ = _tenant(tenant_slug)
        runtime = _runtime()
        return _status_response(
            await runtime.service.get_status(tenant_id), runtime.platform_principals
        )
    except (ApiError, EnvelopeError) as exc:
        logger.warning(
            "envelope.api.error", extra={"tenant_id": tenant_id, "error": type(exc).__name__}
        )
        return _envelope_error(tenant_id, exc)


@tenant_kms_bp.route("/<tenant_slug>/kms", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(KMS_ADMIN_SCOPE)  # type: ignore[untyped-decorator]
@validate_request(KmsConfigRequest)
@validate_response(KmsConfigResponse)
async def put_kms_config(
    data: KmsConfigRequest, tenant_slug: str
) -> KmsConfigResponse | tuple[dict[str, object], int]:
    """Record the customer key as `pending` (Enterprise-gated); moves no key material."""
    tenant_id = 0
    try:
        tenant_id, slug = _tenant(tenant_slug)
        config = await _runtime().service.configure_external_kms(
            tenant_id,
            tenant_slug=slug,
            provider=data.provider,
            key_ref=data.keyRef,
            region=data.region,
            principal=data.principal,
        )
    except (ApiError, EnvelopeError) as exc:
        logger.warning(
            "envelope.api.error", extra={"tenant_id": tenant_id, "error": type(exc).__name__}
        )
        return _envelope_error(tenant_id, exc)
    await _audit(tenant_id, "kms.configure", {"provider": config.provider})
    return KmsConfigResponse(success=True, config=_config_dto(config))


@tenant_kms_bp.route("/<tenant_slug>/kms/activate", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(KMS_ADMIN_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(KmsActivateResponse)
async def activate_kms(
    tenant_slug: str,
) -> KmsActivateResponse | tuple[dict[str, object], int]:
    """Verify the customer key, re-wrap every DEK onto it, and activate (Enterprise-gated)."""
    tenant_id = 0
    try:
        tenant_id, slug = _tenant(tenant_slug)
        config, report = await _runtime().service.activate_external_kms(tenant_id, tenant_slug=slug)
    except (ApiError, EnvelopeError) as exc:
        logger.warning(
            "envelope.api.error", extra={"tenant_id": tenant_id, "error": type(exc).__name__}
        )
        return _envelope_error(tenant_id, exc)
    await _audit(
        tenant_id,
        "kms.activate",
        {"provider": config.provider, "status": config.status, "ok": report.ok},
    )
    return KmsActivateResponse(success=True, config=_config_dto(config), rewrap=_rewrap_dto(report))


@tenant_kms_bp.route("/<tenant_slug>/kms", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(KMS_ADMIN_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(KmsDisableResponse)
async def disable_kms(
    tenant_slug: str,
) -> KmsDisableResponse | tuple[dict[str, object], int]:
    """Exit ramp: re-wrap every DEK back to the platform baseline (never entitlement-gated).

    409 when some DEKs could not be re-wrapped (e.g. the customer key is already
    revoked): the config is kept so they stay addressable, and the body lists them.
    """
    tenant_id = 0
    try:
        tenant_id, _ = _tenant(tenant_slug)
        report = await _runtime().service.disable_external_kms(tenant_id)
    except (ApiError, EnvelopeError) as exc:
        logger.warning(
            "envelope.api.error", extra={"tenant_id": tenant_id, "error": type(exc).__name__}
        )
        return _envelope_error(tenant_id, exc)
    await _audit(tenant_id, "kms.disable", {"ok": report.ok, "failed": len(report.failed_versions)})
    if not report.ok:
        return cast(
            tuple[dict[str, object], int],
            error_response(
                "Some data keys could not be re-wrapped to the platform key; "
                "external KMS stays configured so they remain addressable",
                409,
                "REWRAP_INCOMPLETE",
                {"failedVersions": list(report.failed_versions)},
            ),
        )
    return KmsDisableResponse(success=True, rewrap=_rewrap_dto(report))


BLUEPRINTS: list[Blueprint] = [tenant_kms_bp]
