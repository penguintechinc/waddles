"""v1 tenant-admin DSAR console -- Enterprise access export / erasure / Do-Not-Sell, single + bulk.

The ADMIN layer on top of the statutory self-service DSAR
(`blueprints/v1/data_privacy.py`, `/api/v1/user/me/data`, which is
deliberately UNGATED in every tier and is untouched by this module). A
tenant admin runs a data-subject operation for a caller-supplied user id.
Business rules, tenant fencing, erasure safety rails and the mandatory
audit trail live in `services/admin_data_privacy_service.py` -- read its
module docstring; this file is only the HTTP shell.

Every route, in this order (security.md: tenant -> scope -> feature):

1. `tenant_middleware` -- tenant from the caller's OWN JWT `tenant` claim.
2. `require_scope("tenant:admin")` -- the tenant-admin scope every
   `/api/v1/tenant/<slug>/*` route uses (a global admin's `*:admin`
   covers it). Never role names.
3. `require_matching_tenant` -- the URL slug must equal the JWT tenant.
4. `feature_enabled("waddles.compliance.bulk_dsar")` -- the Enterprise
   two-gate (PostHog flag AND license tier); fails CLOSED (402) when
   either gate is off or unreachable.

Routes (mounted under `/api/v1/tenant`):

| Method | Path                                                  | Action      |
|--------|-------------------------------------------------------|-------------|
| GET    | `/<slug>/privacy/users/<id>/export`                    | export      |
| POST   | `/<slug>/privacy/users/<id>/erase`  `{confirm: true}`  | erase       |
| PUT    | `/<slug>/privacy/users/<id>/do-not-sell`               | do_not_sell |
| POST   | `/<slug>/privacy/bulk` `{action, userIds, confirm}`    | any         |

Responses go through explicit DTOs (security.md Output Validation) via
`services.dto_response.jsonify_dto` -- the same nested-dataclass-after-
`insert_async` quart-schema workaround `data_privacy.py` documents (every
action here writes an audit row first, so the crash precondition always
holds).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import document_response, validate_request

from services import admin_data_privacy_service as svc
from services import tenant_service
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, payment_required
from services.schema import bind_admin_privacy_tables

admin_data_privacy_bp = Blueprint(
    "v1_tenant_admin_data_privacy", __name__, url_prefix="/api/v1/tenant"
)

#: Scope every tenant-admin route in this API requires (`blueprints/v1/tenant.py`).
SCOPE_TENANT_ADMIN = "tenant:admin"

_GATE_DENIED_MESSAGE = "Admin and bulk data-subject requests require an Enterprise plan"

#: Postgres `integer` ids are <= 10 digits; 18 keeps `int()` bounded without a magic 2**31.
_MAX_USER_ID_DIGITS = 18

_HTTP_STATUS_BY_RESULT = {
    svc.DsarStatus.COMPLETED: 200,
    svc.DsarStatus.ALREADY_DONE: 200,
    svc.DsarStatus.NOT_FOUND: 404,
    svc.DsarStatus.CONFLICT: 409,
    svc.DsarStatus.FAILED: 500,
    svc.DsarStatus.AUDIT_UNAVAILABLE: 503,
}


@admin_data_privacy_bp.before_request
async def _ensure_tables() -> None:
    """Idempotently bind this console's tables -- see `schema.bind_admin_privacy_tables`."""
    bind_admin_privacy_tables(current_app.config["dal"])


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class DsarResultDTO:
    """One target user's outcome. `data`/`incomplete` are populated for a completed export only."""

    userId: int
    status: str
    detail: str | None = None
    data: dict[str, list[dict[str, Any]]] | None = None
    incomplete: list[dict[str, str]] | None = None


@dataclass(slots=True, frozen=True)
class SingleDsarResponse:
    """Response DTO for the single-user routes."""

    success: bool
    action: str
    result: DsarResultDTO


@dataclass(slots=True, frozen=True)
class BulkDsarResponse:
    """Response DTO for `POST /<slug>/privacy/bulk` -- always 200 with per-user results."""

    success: bool
    action: str
    requested: int
    succeeded: int
    failed: int
    results: list[DsarResultDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class EraseUserRequest:
    """Request DTO for the single-user erase route."""

    confirm: bool = False


@dataclass(slots=True, frozen=True)
class BulkDsarRequest:
    """Request DTO for the bulk route. `userIds` is capped per action by the service."""

    action: Literal["export", "erase", "do_not_sell"]
    userIds: list[int]
    confirm: bool = False


def _result_dto(result: svc.DsarResult) -> DsarResultDTO:
    return DsarResultDTO(
        userId=result.user_id,
        status=result.status.value,
        detail=result.detail,
        data=result.data,
        incomplete=result.incomplete,
    )


async def _authorize(tenant_slug: str) -> svc.DsarActor:
    """Run the tenant -> slug-match -> Enterprise-gate chain; return the acting admin.

    `tenant_middleware` and `require_scope` have already run (decorators).
    Raises `ApiError` -- 403 on a URL/JWT tenant mismatch, 402 when the
    Enterprise gate denies (fail-closed).
    """
    ctx = get_tenant_context(request)
    # tenant_middleware postcondition (see tenant.py) -- a mypy narrowing aid, not a runtime guard.
    assert ctx is not None  # nosec B101
    tenant_service.require_matching_tenant(tenant_slug, ctx.tenant_slug)
    if not await feature_enabled(svc.FEATURE_BULK_DSAR, tenant=ctx.tenant_slug):
        raise payment_required(_GATE_DENIED_MESSAGE)
    return svc.DsarActor(
        user_id=get_current_user_id(request),
        tenant_id=int(ctx.tenant_id),
        tenant_slug=ctx.tenant_slug,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )


def _parse_user_id(raw: str) -> int:
    """Parse the `<user_id>` path segment into a positive int, or raise a 400 `ApiError`.

    The segment is an untyped Werkzeug converter (not `<int:user_id>`) on
    purpose: quart-schema's `PATH_RE` mis-renders a typed converter that
    follows another parameter (`<tenant_slug>/.../<int:user_id>` ->
    `/api/v1/tenant/{user_id}`), which would publish a wrong OpenAPI path.
    ASCII digits only -- `str.isdigit()` alone accepts other scripts' digits.
    """
    if not (raw.isascii() and raw.isdigit() and len(raw) <= _MAX_USER_ID_DIGITS):
        raise bad_request("user_id must be a positive integer")
    user_id = int(raw)
    if user_id <= 0:
        raise bad_request("user_id must be a positive integer")
    return user_id


async def _run_single(
    tenant_slug: str, action: svc.DsarAction, raw_user_id: str, *, confirm: bool = False
) -> tuple[Any, int]:
    """Authorize, run `action` for one user, and map the outcome onto an HTTP status."""
    async_dal, dal = _dal()
    try:
        actor = await _authorize(tenant_slug)
        user_id = _parse_user_id(raw_user_id)
        results = await svc.run_dsar(
            async_dal, dal, actor=actor, action=action, user_ids=[user_id], confirm=confirm
        )
    except ApiError as exc:
        return _err(exc)
    result = results[0]
    status = _HTTP_STATUS_BY_RESULT[result.status]
    response, _ = jsonify_dto(
        SingleDsarResponse(success=status == 200, action=action.value, result=_result_dto(result)),
        status,
    )
    if action is svc.DsarAction.EXPORT and status == 200:
        response.headers["Content-Disposition"] = (
            f'attachment; filename="waddles-dsar-{user_id}.json"'
        )
    return response, status


@admin_data_privacy_bp.route("/<tenant_slug>/privacy/users/<user_id>/export", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[untyped-decorator]
@document_response(SingleDsarResponse)
async def export_user(tenant_slug: str, user_id: str) -> tuple[Any, int]:
    """Export `user_id`'s personal data held within the admin's own tenant (GDPR Art. 15/20)."""
    return await _run_single(tenant_slug, svc.DsarAction.EXPORT, user_id)


@admin_data_privacy_bp.route("/<tenant_slug>/privacy/users/<user_id>/erase", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[untyped-decorator]
@validate_request(EraseUserRequest)
@document_response(SingleDsarResponse)
async def erase_user(data: EraseUserRequest, tenant_slug: str, user_id: str) -> tuple[Any, int]:
    """Erase/anonymize `user_id` (GDPR Art. 17); needs `confirm=true`, refused if shared."""
    return await _run_single(tenant_slug, svc.DsarAction.ERASE, user_id, confirm=data.confirm)


@admin_data_privacy_bp.route("/<tenant_slug>/privacy/users/<user_id>/do-not-sell", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[untyped-decorator]
@document_response(SingleDsarResponse)
async def do_not_sell_user(tenant_slug: str, user_id: str) -> tuple[Any, int]:
    """Record a CCPA/CPRA Do-Not-Sell opt-out for `user_id` (one-way, idempotent)."""
    return await _run_single(tenant_slug, svc.DsarAction.DO_NOT_SELL, user_id)


@admin_data_privacy_bp.route("/<tenant_slug>/privacy/bulk", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_TENANT_ADMIN)  # type: ignore[untyped-decorator]
@validate_request(BulkDsarRequest)
@document_response(BulkDsarResponse)
async def bulk_dsar(data: BulkDsarRequest, tenant_slug: str) -> tuple[Any, int]:
    """Run one DSAR action over many of the tenant's users -- per-user results, not atomic."""
    async_dal, dal = _dal()
    action = svc.DsarAction(data.action)
    try:
        actor = await _authorize(tenant_slug)
        results = await svc.run_dsar(
            async_dal,
            dal,
            actor=actor,
            action=action,
            user_ids=data.userIds,
            confirm=data.confirm,
        )
    except ApiError as exc:
        return _err(exc)
    ok = sum(
        1 for r in results if r.status in (svc.DsarStatus.COMPLETED, svc.DsarStatus.ALREADY_DONE)
    )
    return jsonify_dto(
        BulkDsarResponse(
            success=ok == len(results),
            action=action.value,
            requested=len(results),
            succeeded=ok,
            failed=len(results) - ok,
            results=[_result_dto(r) for r in results],
        )
    )


BLUEPRINTS: list[Blueprint] = [admin_data_privacy_bp]
