"""v1 `platform_secrets` admin group -- operator rotation / retirement of the platform's own keys.

Mount: `/api/v1/admin/platform-secrets`.

  GET  /                       status of every registered type (versions, states, windows)
  POST /<type>/rotate          mint version N+1 ACTIVE; the previous one becomes verify-only
  POST /<type>/retire          retire one verify-only version once its window has closed

`<type>` is the registry name (`jwt_signing`, `bundle_signing`, `precompile_signing`) or its int
code. Rotation is operator-triggered ONLY; upgrades never rotate (see `rotation.py`).

Who may call (security review H7), all fail closed and every refusal below the scope gate is
audited in `platform_vault.secret_events` (actor UUID, int codes, never a value):

  1. `tenant_middleware` (outermost) then `require_scope("platform_secrets:admin")` -- scopes only,
     never role names; the scope gate's own denials reach the platform audit log via
     `request.authz_decision`.
  2. The token's tenant is the reserved `system` tenant AND the caller is a global admin (a tenant
     admin of any other tenant is refused even when its token carries the scope).
  3. CSRF: a request authenticated by the `wb_session` COOKIE (SameSite=Lax, see
     `services/session_cookie.py`) must also carry `X-Requested-With: waddles-admin` and a JSON
     body, and any `Origin` header must be this origin -- a cross-site form/fetch cannot set a
     custom header without a CORS preflight. A bearer-header caller (the CLI) is not exposed.
  4. Recent MFA: `amr` carries a second factor and `auth_time` is within 5 minutes
     (`services/mfa_stepup.py`).
  5. `Idempotency-Key: <uuid>` is mandatory and single-use (replay ledger); rotate is also limited
     to one new version per type per 10 minutes, and retire is refused until the type's verify
     window -- computed server-side -- has closed.

Responses are explicit DTOs (`security.md` Output Validation): version numbers, int state codes and
timestamps only. No secret value, fingerprint, key id or ciphertext ever leaves the process.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import Field
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response
from sqlalchemy.exc import SQLAlchemyError

from services import global_identity, mfa_stepup
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.platform_secrets import rotation, telemetry
from services.platform_secrets.bootstrap import db_platform_keys_disabled
from services.platform_secrets.registry import SecretSpec
from services.provider_connections.vault import PlatformSecretError
from services.session_cookie import SESSION_COOKIE_NAME

logger = logging.getLogger(__name__)

platform_secrets_bp = Blueprint(
    "v1_platform_secrets", __name__, url_prefix="/api/v1/admin/platform-secrets"
)

#: The scope this group requires (`resource:action`; carried by the global-admin bundle).
PLATFORM_SECRETS_SCOPE = "platform_secrets:admin"
#: Header a cookie-authenticated caller must send (and its required value).
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "waddles-admin"
IDEMPOTENCY_HEADER = "Idempotency-Key"


@dataclass(slots=True, frozen=True)
class RotateRequest:
    """Request DTO for `POST /<type>/rotate`: only an operator reason code (1/2/3)."""

    reasonCode: Annotated[int, Field(ge=1, le=3)]


@dataclass(slots=True, frozen=True)
class RetireRequest:
    """Request DTO for `POST /<type>/retire`: the verify-only version and a reason code."""

    version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    reasonCode: Annotated[int, Field(ge=1, le=3)]


@dataclass(slots=True, frozen=True)
class RotateResponse:
    """Response DTO: the new active version and when the previous one stops verifying."""

    success: bool
    type: str
    newVersion: int
    previousVersion: int
    previousVerifyUntil: str | None


@dataclass(slots=True, frozen=True)
class RetireResponse:
    """Response DTO: which version was retired and when."""

    success: bool
    type: str
    version: int
    retiredAt: str


@dataclass(slots=True, frozen=True)
class VersionDTO:
    """Response DTO: one version (no key id, fingerprint or value).

    `state` is the int code: 1 pending, 2 active, 3 verify-only, 4 retired, 5 destroyed.
    """

    version: int
    state: int
    activatedAt: str | None
    rotatedAt: str | None
    retiredAt: str | None
    verifyUntil: str | None


@dataclass(slots=True, frozen=True)
class TypeDTO:
    """Response DTO: one registered secret type and its versions."""

    type: str
    typeCode: int
    versions: list[VersionDTO]


@dataclass(slots=True, frozen=True)
class StatusResponse:
    """Response DTO for `GET /`."""

    success: bool
    types: list[TypeDTO]


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 UTC text or None."""
    return value.astimezone(UTC).isoformat() if value else None


def _err(status: int, code: str, message: str) -> tuple[dict[str, object], int]:
    """A typed error response (stable `code`, fixed message)."""
    return cast(tuple[dict[str, object], int], error_response(message, status, code))


def _actor_uuid_sync(dal: Any, user_id: int) -> uuid.UUID | None:
    """`hub_users.uuid` of the caller, or None when it cannot be resolved."""
    users = dal.hub_users
    if "uuid" not in users.fields:
        return None
    row = dal(users.id == user_id).select(users.uuid, limitby=(0, 1)).first()
    return uuid.UUID(str(row.uuid)) if row is not None and row.uuid else None


async def _actor_uuid(user_id: int) -> uuid.UUID | None:
    """Resolve the caller's UUID off the event loop."""
    return await asyncio.to_thread(_actor_uuid_sync, current_app.config["dal"], user_id)


async def _is_global_admin(user_id: int) -> bool:
    """True iff the caller is a global admin (admin of the `system` tenant)."""
    return await global_identity.is_global_admin(
        current_app.config["async_dal"], current_app.config["dal"], user_id=user_id
    )


def _factory() -> rotation.SessionFactory:
    """The locked vault session factory (SQL by default; tests install a fake)."""
    cfg = current_app.config["HUB_API_CONFIG"]
    return rotation.get_session_factory(cfg.database_url)


async def _run[T](fn: Callable[[Any, Any], T]) -> T:
    """Run `fn(repo, kek)` in a worker thread inside one locked, transactional vault session."""

    def work() -> T:
        factory = _factory()
        kek = rotation.get_kek()
        with factory() as repo:
            return fn(repo, kek)

    return await asyncio.to_thread(work)


async def _deny(
    spec: SecretSpec,
    actor: uuid.UUID,
    reason: rotation.RotationReason,
    code: str,
    status: int,
    message: str,
) -> tuple[dict[str, object], int]:
    """Audit a refusal in `secret_events` (best effort: never masks the refusal) and answer."""
    try:
        await _run(
            lambda repo, kek: rotation.record_denial(
                repo, kek, spec=spec, actor=actor, reason=reason, code=code
            )
        )
    except (PlatformSecretError, SQLAlchemyError, RuntimeError, ValueError) as exc:
        logger.error("platform secret denial audit failed (%s)", type(exc).__name__)
    return _err(status, code.upper(), message)


def _cookie_authenticated() -> bool:
    """True when this request's bearer token came from the session cookie (not a client header)."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    header = request.headers.get("Authorization", "")
    return bool(cookie) and header == f"Bearer {cookie}"


def csrf_failure() -> str | None:
    """Why a cookie-authenticated request fails the CSRF defence (None = ok / bearer caller)."""
    if not _cookie_authenticated():
        return None
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return "missing_header"
    if (request.mimetype or "") != "application/json":
        return "not_json"
    origin = request.headers.get("Origin")
    if origin is not None and origin != request.host_url.rstrip("/"):
        return "bad_origin"
    return None


@dataclass(slots=True, frozen=True)
class _Caller:
    """An authorised caller and the spec being acted on."""

    spec: SecretSpec
    actor: uuid.UUID
    idempotency_key: uuid.UUID


async def _authorise(
    secret_type: str, reason: rotation.RotationReason
) -> _Caller | tuple[dict[str, object], int]:
    """Run every gate below the scope check; a refusal is audited and returned as the response."""
    spec = rotation.resolve_type(secret_type)
    if spec is None:
        return _err(404, "UNKNOWN_SECRET_TYPE", "No such platform secret type")
    try:
        user_id = get_current_user_id(request)
    except ApiError as exc:
        return cast(
            tuple[dict[str, object], int],
            error_response(exc.message, exc.status_code, exc.code, exc.details),
        )
    actor = await _actor_uuid(user_id)
    if actor is None:
        return _err(403, "ACTOR_UNRESOLVED", "Caller identity could not be resolved")
    ctx = get_tenant_context(request)
    if ctx is None or ctx.tenant_slug != global_identity.SYSTEM_TENANT_SLUG:
        return await _deny(
            spec, actor, reason, "not_system_tenant", 403, "System tenant admins only"
        )
    if not await _is_global_admin(user_id):
        return await _deny(spec, actor, reason, "not_global_admin", 403, "Global admins only")
    if csrf_failure() is not None:
        return await _deny(spec, actor, reason, "csrf_rejected", 403, "CSRF check failed")
    claims = mfa_stepup.request_claims(request)
    if claims is None or mfa_stepup.step_up_failure(claims) is not None:
        return await _deny(
            spec, actor, reason, "mfa_required", 403, "Recent multi-factor authentication required"
        )
    try:
        key = uuid.UUID(request.headers.get(IDEMPOTENCY_HEADER, ""))
    except ValueError:
        return await _deny(
            spec, actor, reason, "bad_request", 400, "A UUID Idempotency-Key header is required"
        )
    return _Caller(spec, actor, key)


def _refusal_response(refusal: rotation.Refusal) -> tuple[dict[str, object], int]:
    """HTTP form of a business refusal (stable code, fixed text)."""
    return _err(refusal.status, refusal.code.upper(), f"Request refused: {refusal.code}")


def _unavailable(exc: Exception) -> tuple[dict[str, object], int]:
    """503 for an infrastructure failure; only the exception class is logged."""
    logger.error("platform secret vault unavailable (%s)", type(exc).__name__)
    return _err(503, "VAULT_UNAVAILABLE", "The platform secret vault is unavailable")


def _disabled() -> tuple[dict[str, object], int] | None:
    """409 while the DB-platform-keys kill-switch is ON (the legacy sources are authoritative)."""
    if db_platform_keys_disabled():
        return _err(409, "VAULT_DISABLED", "Platform keys are served from the legacy sources")
    return None


@platform_secrets_bp.route("/", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(PLATFORM_SECRETS_SCOPE)  # type: ignore[misc]
@validate_response(StatusResponse)
async def status() -> StatusResponse | tuple[dict[str, object], int]:
    """No-secrets status of every platform secret type (system-tenant global admins only)."""
    ctx = get_tenant_context(request)
    if ctx is None or ctx.tenant_slug != global_identity.SYSTEM_TENANT_SLUG:
        return _err(403, "NOT_SYSTEM_TENANT", "System tenant admins only")
    try:
        user_id = get_current_user_id(request)
    except ApiError as exc:
        return cast(
            tuple[dict[str, object], int],
            error_response(exc.message, exc.status_code, exc.code, exc.details),
        )
    if not await _is_global_admin(user_id):
        return _err(403, "NOT_GLOBAL_ADMIN", "Global admins only")
    try:
        rows = await _run(lambda repo, _kek: rotation.list_status(repo))
    except (PlatformSecretError, SQLAlchemyError, RuntimeError) as exc:
        return _unavailable(exc)
    return StatusResponse(
        success=True,
        types=[
            TypeDTO(
                type=t.name,
                typeCode=t.secret_type,
                versions=[
                    VersionDTO(
                        version=v.version,
                        state=v.state,
                        activatedAt=_iso(v.activated_at),
                        rotatedAt=_iso(v.rotated_at),
                        retiredAt=_iso(v.retired_at),
                        verifyUntil=_iso(v.verify_until),
                    )
                    for v in t.versions
                ],
            )
            for t in rows
        ],
    )


@platform_secrets_bp.route("/<secret_type>/rotate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(PLATFORM_SECRETS_SCOPE)  # type: ignore[misc]
@validate_request(RotateRequest)
@validate_response(RotateResponse)
async def rotate(
    secret_type: str, data: RotateRequest
) -> RotateResponse | tuple[dict[str, object], int]:
    """Mint the next version ACTIVE and demote the current one to verify-only."""
    if (blocked := _disabled()) is not None:
        return blocked
    reason = rotation.RotationReason(data.reasonCode)
    caller = await _authorise(secret_type, reason)
    if not isinstance(caller, _Caller):
        return caller
    try:
        result = await _run(
            lambda repo, kek: rotation.rotate_secret(
                repo,
                kek,
                spec=caller.spec,
                actor=caller.actor,
                reason=reason,
                idempotency_key=caller.idempotency_key,
            )
        )
    except (PlatformSecretError, SQLAlchemyError, RuntimeError) as exc:
        telemetry.count_rotation(caller.spec.name, "error")
        return _unavailable(exc)
    if isinstance(result, rotation.Refusal):
        return _refusal_response(result)
    factory, kek = _factory(), rotation.get_kek()
    await asyncio.to_thread(rotation.escrow_after_change, factory, kek)
    return RotateResponse(
        success=True,
        type=result.type_name,
        newVersion=result.new_version,
        previousVersion=result.previous_version,
        previousVerifyUntil=_iso(result.verify_until),
    )


@platform_secrets_bp.route("/<secret_type>/retire", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(PLATFORM_SECRETS_SCOPE)  # type: ignore[misc]
@validate_request(RetireRequest)
@validate_response(RetireResponse)
async def retire(
    secret_type: str, data: RetireRequest
) -> RetireResponse | tuple[dict[str, object], int]:
    """Retire one verify-only version after the verify window (server-side) has closed."""
    if (blocked := _disabled()) is not None:
        return blocked
    reason = rotation.RotationReason(data.reasonCode)
    caller = await _authorise(secret_type, reason)
    if not isinstance(caller, _Caller):
        return caller
    try:
        result = await _run(
            lambda repo, kek: rotation.retire_secret(
                repo,
                kek,
                spec=caller.spec,
                version=data.version,
                actor=caller.actor,
                reason=reason,
                idempotency_key=caller.idempotency_key,
            )
        )
    except (PlatformSecretError, SQLAlchemyError, RuntimeError) as exc:
        telemetry.count_rotation(caller.spec.name, "error")
        return _unavailable(exc)
    if isinstance(result, rotation.Refusal):
        return _refusal_response(result)
    factory, kek = _factory(), rotation.get_kek()
    await asyncio.to_thread(rotation.escrow_after_change, factory, kek)
    return RetireResponse(
        success=True,
        type=result.type_name,
        version=result.version,
        retiredAt=_iso(result.retired_at) or "",
    )


BLUEPRINTS: list[Blueprint] = [platform_secrets_bp]
