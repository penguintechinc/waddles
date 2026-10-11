"""v1 `user passkey` group -- ported from `passkeyController.js`.

Only the credential-management routes (`register/start`, `register/finish`,
`credentials` list/delete) -- mounted at `/api/v1/user/passkey` per
`routes/passkeys.js`. The login routes (`/api/v1/auth/passkey/login/
start|finish`) live in `blueprints/v1/auth.py`, matching Node's own
`routes/passkeys.js` mounting both prefixes from the one controller file;
see that blueprint's module docstring. All routes here are self-service
(a user managing their own passkeys) -- `tenant_middleware` only, no
`require_scope`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import passkey_service
from services.current_user import get_current_session_id, get_current_user_id
from services.errors import ApiError

passkey_bp = Blueprint("v1_user_passkey", __name__, url_prefix="/api/v1/user/passkey")

logger = logging.getLogger(__name__)


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class PasskeyOptionsResponse:
    """Response DTO for passkey options endpoints."""

    success: bool
    options: dict[str, Any]


@dataclass(slots=True, frozen=True)
class FinishRegistrationRequest:
    """Request DTO for finish registration endpoints."""

    credential: dict[str, Any]
    deviceName: str | None = None


@dataclass(slots=True, frozen=True)
class SimpleSuccessResponse:
    """Response DTO for simple success endpoints."""

    success: bool


@dataclass(slots=True, frozen=True)
class CredentialDTO:
    """Credential DTO."""

    id: int
    deviceName: str | None
    createdAt: str | None
    lastUsedAt: str | None


@dataclass(slots=True, frozen=True)
class ListCredentialsResponse:
    """Response DTO for list credentials endpoints."""

    success: bool
    credentials: list[CredentialDTO]


@passkey_bp.route("/register/start", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(PasskeyOptionsResponse)
async def start_registration() -> PasskeyOptionsResponse | tuple[dict[str, object], int]:
    """Start registration."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        options = await passkey_service.start_registration(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return PasskeyOptionsResponse(success=True, options=options)


@passkey_bp.route("/register/finish", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(FinishRegistrationRequest)
@validate_response(SimpleSuccessResponse)
async def finish_registration(
    data: FinishRegistrationRequest,
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Finish registration."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await passkey_service.finish_registration(
            async_dal, dal, user_id=user_id, credential=data.credential, device_name=data.deviceName
        )
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


@passkey_bp.route("/credentials", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(ListCredentialsResponse)
async def list_credentials() -> ListCredentialsResponse:
    """List credentials."""
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    rows = await passkey_service.list_credentials(async_dal, dal, user_id=user_id)
    return ListCredentialsResponse(
        success=True,
        credentials=[
            CredentialDTO(
                id=r.id,
                deviceName=r.device_name,
                createdAt=r.created_at.isoformat() if r.created_at else None,
                lastUsedAt=r.last_used_at.isoformat() if r.last_used_at else None,
            )
            for r in rows
        ],
    )


@passkey_bp.route("/credentials/<int:credential_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(SimpleSuccessResponse)
async def remove_credential(
    credential_id: int,
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Remove credential."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await passkey_service.remove_credential(
            async_dal,
            dal,
            user_id=user_id,
            credential_pk_id=credential_id,
            keep_session_id=get_current_session_id(request),
        )
    except ApiError as exc:
        logger.info("%s refused: status=%s code=%s", "remove_credential", exc.status_code, exc.code)
        return _err(exc)
    return SimpleSuccessResponse(success=True)


BLUEPRINTS: list[Blueprint] = [passkey_bp]
