"""v1 `one_time_secrets` group -- create + single-use pull (feature #684, `!secret`).

`POST /api/v1/one-time-secrets` (scope `secret_messaging:create`, bundle/service
caller) stores an encrypted message for a target `hub_users.uuid` and returns
the link token once. `POST /api/v1/one-time-secrets/pull` (scope
`secret_messaging:pull`) lets only the linked target user pull it, once. The
token travels in the POST body (never a URL/query) so it stays out of access
logs. Tenant comes from the JWT only. Behind PostHog flag
`waddles.secret-messaging` (default off). See `services/one_time_secret_service.py`
for the encryption / atomic-claim / PII-free-log guarantees.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from pydantic import ConfigDict
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_request, validate_response

from services import one_time_secret_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError

FEATURE_SECRET_MESSAGING = "waddles.secret-messaging"  # noqa: S105 -- flag key, not a credential

one_time_secrets_bp = Blueprint(
    "v1_one_time_secrets", __name__, url_prefix="/api/v1/one-time-secrets"
)


@one_time_secrets_bp.after_request
async def _no_store(response: Response) -> Response:
    """Secrets and tokens must never be cached by a browser or proxy."""
    response.headers["Cache-Control"] = "no-store"
    return response


@dataclass(slots=True, frozen=True)
class CreateSecretRequest:
    """Body for create. Target is a `hub_users.uuid` -- never a username."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    communityId: int
    targetUserUuid: uuid.UUID
    message: str
    ttlSeconds: int = svc.DEFAULT_TTL_SECONDS


@dataclass(slots=True, frozen=True)
class CreateSecretResponse:
    """Create result; `token` is the only time the link token is ever returned."""

    success: bool
    secretId: str
    token: str
    expiresAt: str


@dataclass(slots=True, frozen=True)
class PullSecretRequest:
    """Body for pull."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    token: str


@dataclass(slots=True, frozen=True)
class PullSecretResponse:
    """Pull result -- the secret message, delivered once."""

    success: bool
    message: str


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _tenant() -> tuple[int, str]:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware always runs first
    return cast(int, ctx.tenant_id), cast(str, ctx.tenant_slug)


async def _flag_off(tenant_slug: str) -> tuple[dict[str, object], int] | None:
    if await feature_enabled(FEATURE_SECRET_MESSAGING, tenant=tenant_slug):
        return None
    return _err(ApiError("Secret messaging is not enabled", 404, "FEATURE_DISABLED"))


@one_time_secrets_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("secret_messaging:create")  # type: ignore[untyped-decorator]
@validate_request(CreateSecretRequest)
@validate_response(CreateSecretResponse, status_code=201)
async def create_secret(
    data: CreateSecretRequest,
) -> tuple[CreateSecretResponse | dict[str, object], int]:
    """Store an encrypted one-time secret for the target user; return the link token once."""
    tenant_id, slug = _tenant()
    if (off := await _flag_off(slug)) is not None:
        return off
    try:
        created = await svc.create_secret(
            cast(AsyncDB, current_app.config["install_dal"]),
            tenant_id=tenant_id,
            community_id=data.communityId,
            target_user_uuid=data.targetUserUuid,
            message=data.message,
            ttl_seconds=data.ttlSeconds,
        )
    except ApiError as exc:
        return _err(exc)
    return (
        CreateSecretResponse(
            success=True,
            secretId=created.secret_id,
            token=created.token,
            expiresAt=created.expires_at.isoformat(),
        ),
        201,
    )


@one_time_secrets_bp.route("/pull", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("secret_messaging:pull")  # type: ignore[untyped-decorator]
@validate_request(PullSecretRequest)
@validate_response(PullSecretResponse)
async def pull_secret(
    data: PullSecretRequest,
) -> PullSecretResponse | tuple[dict[str, object], int]:
    """Return the secret once to the linked target user, then delete it."""
    tenant_id, slug = _tenant()
    if (off := await _flag_off(slug)) is not None:
        return off
    try:
        message = await svc.pull_secret(
            cast(AsyncDB, current_app.config["install_dal"]),
            tenant_id=tenant_id,
            caller_user_id=get_current_user_id(request),
            token=data.token,
        )
    except ApiError as exc:
        return _err(exc)
    return PullSecretResponse(success=True, message=message)


BLUEPRINTS: list[Blueprint] = [one_time_secrets_bp]
