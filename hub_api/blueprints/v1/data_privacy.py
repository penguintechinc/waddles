"""v1 `data privacy` (GDPR/CCPA DSAR) group -- ported from `dataPrivacyController.js`.

Mounted at the exact Node path, `/api/v1/user/me/data`
(`routes/user.js`, gated there by that router's top-level `router.use(
requireAuth)`). Both routes here are self-service --
`@tenant_middleware` only, subject resolved via `services.
current_user.get_current_user_id`, per `hub_api/PORTING.md`'s Auth
pattern table. There is deliberately no request parameter anywhere in
this file that can name a user id other than the caller's own -- see
`services/data_privacy_service.py`'s module docstring for why that is
this group's entire IDOR/BOLA mitigation.

Neither route uses `@validate_response`: `export_user_data` builds a
dynamic, per-source dict of rows (`services/data_privacy_service.py`'s
`EXPORT_SOURCES`-equivalent) and also needs to set a `Content-
Disposition` header, which `@validate_response`'s auto-generated
`Response` gives no hook for; `request_data_deletion` has two possible
200-status response shapes (`already_deleted` vs `deleted`), which
`@validate_response` can't express as cleanly as `auth.py`'s 200-vs-403
dual-decorator pattern (both branches share one status here). `services.
dto_response.jsonify_dto()` gives full control over both.

Table binding: `services.schema.bind_privacy_tables()` is called from
this blueprint's own `before_request` hook, not from `app.py::
_bind_reference_tables()` (the pattern `hub_api/PORTING.md`'s checklist
step 2 describes) -- this port PR is scoped to never touch `app.py`/
`routers/*.py`/`blueprints/__init__.py` (shared collision points for the
parallel M-phase port wave). `bind_privacy_tables()` is idempotent, so a
per-request hook is a correctness-equivalent, effectively-zero-cost
substitute once the first request lands; see that function's own
docstring for the exact rationale and the follow-up note for whoever
next touches `app.py` for an unrelated reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request

from services import data_privacy_service as svc
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.schema import bind_privacy_tables

data_privacy_bp = Blueprint("v1_user_data_privacy", __name__, url_prefix="/api/v1/user/me/data")


@data_privacy_bp.before_request
async def _ensure_tables() -> None:
    """Idempotently bind this group's tables -- see module docstring."""
    bind_privacy_tables(current_app.config["dal"])


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class ExportUserDataResponse:
    """Response DTO for `GET /api/v1/user/me/data`.

    See module docstring for the field-set rationale.
    """

    success: bool
    exported_at: str
    subject_id: int
    data: dict[str, list[dict[str, Any]]]
    incomplete: list[dict[str, str]] | None = None


@dataclass(slots=True, frozen=True)
class RequestDataDeletionRequest:
    """Request DTO for `DELETE /api/v1/user/me/data`.

    `password` is only required if the account has one.
    """

    password: str | None = None


@dataclass(slots=True, frozen=True)
class AlreadyDeletedResponse:
    """Response DTO for the already-deleted short-circuit branch."""

    success: bool
    already_deleted: bool


@dataclass(slots=True, frozen=True)
class DeletionSuccessResponse:
    """Response DTO for a newly-completed deletion."""

    success: bool
    deleted: bool


@data_privacy_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def export_user_data() -> tuple[Any, int]:
    """Export the authenticated user's personal data (GDPR Art. 15/20)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        data, failures = await svc.export_user_data(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    response, status = jsonify_dto(
        ExportUserDataResponse(
            success=True,
            exported_at=datetime.now(UTC).isoformat(),
            subject_id=user_id,
            data=data,
            incomplete=failures or None,
        )
    )
    response.headers["Content-Disposition"] = f'attachment; filename="waddles-data-{user_id}.json"'
    return response, status


@data_privacy_bp.route("", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_request(RequestDataDeletionRequest)
async def request_data_deletion(data: RequestDataDeletionRequest) -> tuple[Any, int]:
    """Anonymize and delete personal data for the authenticated user (GDPR Art. 17)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        already_deleted, deleted = await svc.request_data_deletion(
            async_dal, dal, user_id=user_id, password=data.password
        )
    except ApiError as exc:
        return _err(exc)
    if already_deleted:
        return jsonify_dto(AlreadyDeletedResponse(success=True, already_deleted=True))
    return jsonify_dto(DeletionSuccessResponse(success=True, deleted=deleted))


BLUEPRINTS: list[Blueprint] = [data_privacy_bp]
