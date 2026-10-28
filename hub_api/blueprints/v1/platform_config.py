"""v1 `superadmin platform-config` group -- ported from `platformConfigController.js`.

Mounted at `/api/v1/superadmin/platform-config*` and `/api/v1/superadmin/
settings`, matching the subset of `routes/superadmin.js` this group owns
(see `services/platform_config_service.py`'s module docstring for the
exact scope boundary against `adminController`/self-service oauth
credentials, which are NOT ported here).

Gated by `require_scope("users:admin")` -- the SAME scope
`blueprints/v1/user_management.py` uses for its own superadmin.js-mounted
group, and for the identical reason: both are gated in Node by
`router.use(requireSuperAdmin)` at the top of `superadmin.js`, so both
translate to the same OIDC-native equivalent (present only in
`SCOPE_BUNDLES["global"]["admin"]`, granted exactly when `hub_users.
is_super_admin` is true -- see `user_management.py`'s own docstring).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import platform_config_service as svc
from services.errors import ApiError

platform_config_bp = Blueprint(
    "v1_superadmin_platform_config", __name__, url_prefix="/api/v1/superadmin"
)


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class CredentialDTO:
    """Credential DTO -- masks token/secret fields, mirrors Node's `formatCredential()`."""

    id: int
    platform: str
    integrationType: str
    communityId: int | None
    userId: int | None
    accessToken: str | None
    refreshToken: str | None
    clientId: str | None
    clientSecret: str | None
    tokenType: str | None
    expiresAt: str | None
    scopes: list[str]
    configData: dict[str, Any] | None
    isActive: bool
    isEncrypted: bool
    createdAt: str | None
    updatedAt: str | None
    createdByUserId: int | None
    updatedByUserId: int | None


@dataclass(slots=True, frozen=True)
class ListPlatformConfigsResponse:
    """Response DTO for list platform configs."""

    success: bool
    data: list[CredentialDTO] = field(default_factory=list)
    count: int = 0


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for `{success, message}`/`{success, error}` endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class UpdatePlatformConfigRequest:
    """Request DTO for update platform config -- see service docstring: always 404s."""

    accessToken: str | None = None
    refreshToken: str | None = None
    clientId: str | None = None
    clientSecret: str | None = None
    expiresAt: str | None = None
    scopes: list[str] | None = None
    configData: dict[str, Any] | None = None
    isActive: bool | None = None


@dataclass(slots=True, frozen=True)
class TestConnectionDataDTO:
    """Test connection data DTO."""

    platform: str
    valid: bool
    error: str | None
    testedAt: str


@dataclass(slots=True, frozen=True)
class TestConnectionResponse:
    """Response DTO for test platform connection."""

    success: bool
    data: TestConnectionDataDTO


@dataclass(slots=True, frozen=True)
class HubSettingsResponse:
    """Response DTO for hub settings."""

    success: bool
    data: dict[str, str]


def _credential_dto(row: svc.CredentialRow) -> CredentialDTO:
    return CredentialDTO(
        id=row.id,
        platform=row.platform,
        integrationType=row.integration_type,
        communityId=row.community_id,
        userId=row.user_id,
        accessToken="***" if row.access_token_set else None,
        refreshToken="***" if row.refresh_token_set else None,
        clientId=row.client_id,
        clientSecret="***" if row.client_secret_set else None,
        tokenType=row.token_type,
        expiresAt=_iso(row.expires_at),
        scopes=row.scopes,
        configData=row.config_data,
        isActive=row.is_active,
        isEncrypted=row.is_encrypted,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
        createdByUserId=row.created_by_user_id,
        updatedByUserId=row.updated_by_user_id,
    )


@platform_config_bp.route("/platform-config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(ListPlatformConfigsResponse)
async def get_platform_configs() -> ListPlatformConfigsResponse:
    """Get platform configs."""
    async_dal, dal = _dal()
    integration_type = request.args.get("integrationType")
    platform = request.args.get("platform")
    rows = await svc.get_platform_configs(
        async_dal, dal, integration_type=integration_type, platform=platform
    )
    dtos = [_credential_dto(r) for r in rows]
    return ListPlatformConfigsResponse(success=True, data=dtos, count=len(dtos))


@platform_config_bp.route("/platform-config/<platform>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_request(UpdatePlatformConfigRequest)
@validate_response(MessageResponse)
async def update_platform_config(
    data: UpdatePlatformConfigRequest, platform: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update platform config.

    See `services/platform_config_service.update_platform_config()`'s
    docstring -- this always 404s, matching a pre-existing Node routing
    bug, not a regression introduced by this port.
    """
    async_dal, dal = _dal()
    try:
        await svc.update_platform_config(async_dal, dal, platform=platform)
    except ApiError as exc:
        return _err(exc)
    # pragma: no cover -- unreachable, svc.update_platform_config() always raises.
    return MessageResponse(success=True, message="Platform configuration updated")


@platform_config_bp.route("/platform-config/<platform>/test", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(TestConnectionResponse)
async def test_platform_connection(
    platform: str,
) -> TestConnectionResponse | tuple[dict[str, object], int]:
    """Test platform connection."""
    async_dal, dal = _dal()
    try:
        valid, error = await svc.test_platform_connection(async_dal, dal, platform=platform)
    except ApiError as exc:
        return _err(exc)
    return TestConnectionResponse(
        success=True,
        data=TestConnectionDataDTO(
            platform=platform, valid=valid, error=error, testedAt=datetime.now(UTC).isoformat()
        ),
    )


@platform_config_bp.route("/settings", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(HubSettingsResponse)
async def get_hub_settings() -> HubSettingsResponse:
    """Get hub settings."""
    async_dal, dal = _dal()
    settings = await svc.get_hub_settings(async_dal, dal)
    return HubSettingsResponse(success=True, data=settings)


@platform_config_bp.route("/settings", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("users:admin")  # type: ignore[misc]
@validate_response(HubSettingsResponse)
async def update_hub_settings() -> tuple[Any, int]:
    """Update hub settings.

    NOT `@validate_request` (the response IS validated against
    `HubSettingsResponse`) -- Node's `updateHubSettings` accepts an
    arbitrary flat key/value body (`for (const [key, value] of
    Object.entries(req.body))`), not a fixed shape; quart-schema's
    dataclass-based `@validate_request` has no "arbitrary extra keys"
    escape hatch the way Node's untyped `req.body` does. Reads the raw
    JSON body directly instead, matching Node's own behavior byte-for-byte.
    """
    async_dal, dal = _dal()
    updates = await request.get_json(force=True)
    if not isinstance(updates, dict):
        return cast(
            tuple[dict[str, object], int],
            error_response("Request body must be a JSON object", 400, "BAD_REQUEST"),
        )
    settings = await svc.update_hub_settings(async_dal, dal, updates=updates)
    return HubSettingsResponse(success=True, data=settings), 200


BLUEPRINTS: list[Blueprint] = [platform_config_bp]
