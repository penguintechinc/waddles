"""v1 media (Gemini) BYOK -- community-admin key API.

Spec: `docs/superpowers/specs/2026-11-06-harness-media-generation.md` (PR3).

* `media_byok_admin_bp` (`/api/v1/admin/<community_id>/media/byok-keys`) -- set/rotate, status,
  delete. Community-admin only (`require_community_admin`, tenant-bound), Professional-or-higher
  required to SET (a downgraded community can still delete). The key is never returned: responses
  carry `key_last4` only. Audited by the HTTP audit hook (`audit_events._INLINE_AUTHZ_GROUPS`).

The Rust media host's resolver/tier reads are gRPC, not REST: `MediaService` in
`grpc_internal.dataplane_servicers` (`proto/waddles/hub/internal/v1/media.proto`) -- the caller
identity is allow-listed, every resolve is rate-limited per (caller, community) and audited
(who/when/outcome, never the key).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import media_byok_service as media
from services.community_access import require_community_admin
from services.current_user import get_current_user_id
from services.errors import ApiError

logger = logging.getLogger(__name__)

media_byok_admin_bp = Blueprint("v1_media_byok_admin", __name__, url_prefix="/api/v1/admin")


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class MediaByokKeyDTO:
    """Masked key record -- never carries the real key."""

    provider: str
    key_last4: str
    is_active: bool
    created_at: str | None
    updated_at: str | None
    rotated_at: str | None


@dataclass(slots=True, frozen=True)
class MediaByokStatusResponse:
    """`GET` response: `configured` is true iff an ACTIVE key is on file."""

    success: bool
    configured: bool
    keys: list[MediaByokKeyDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SetMediaByokRequest:
    """`PUT` body -- set-or-rotate one provider's key."""

    provider: str
    api_key: str


@dataclass(slots=True, frozen=True)
class MediaMessageResponse:
    """Generic `{success, message}` response."""

    success: bool
    message: str


def _key_dto(info: Any) -> MediaByokKeyDTO:
    return MediaByokKeyDTO(
        provider=info.provider,
        key_last4=info.key_last4,
        is_active=info.is_active,
        created_at=_iso(info.created_at),
        updated_at=_iso(info.updated_at),
        rotated_at=_iso(info.rotated_at),
    )


async def _require_admin(community_id: int) -> tuple[int, int]:
    """Enforce community-admin inside the caller's tenant; returns `(tenant_id, user_id)`."""
    ctx = get_tenant_context(request)
    if ctx is None or ctx.tenant_id is None:
        raise ApiError("Tenant context not resolved", 403, "FORBIDDEN")
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    await require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return int(ctx.tenant_id), user_id


@media_byok_admin_bp.route("/<int:community_id>/media/byok-keys", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MediaByokStatusResponse)
async def get_media_byok_status(
    community_id: int,
) -> MediaByokStatusResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<community_id>/media/byok-keys` -- masked status, never the key."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _require_admin(community_id)
        keys = await media.media_key_status(
            async_dal, dal, tenant_id=tenant_id, community_id=community_id
        )
    except ApiError as exc:
        logger.warning("%s refused: %s: %s", request.path, type(exc).__name__, exc.message)
        return _err(exc)
    return MediaByokStatusResponse(
        success=True,
        configured=any(k.is_active for k in keys),
        keys=[_key_dto(k) for k in keys],
    )


@media_byok_admin_bp.route("/<int:community_id>/media/byok-keys", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(SetMediaByokRequest)
@validate_response(MediaByokKeyDTO)
async def set_media_byok_key(
    data: SetMediaByokRequest, community_id: int
) -> MediaByokKeyDTO | tuple[dict[str, object], int]:
    """`PUT .../media/byok-keys` -- Professional+ required; validated against Gemini first.

    Never logs or echoes `data.api_key`; the response is the masked record.
    """
    async_dal, dal = _dal()
    try:
        tenant_id, user_id = await _require_admin(community_id)
        info = await media.set_media_key(
            async_dal,
            dal,
            tenant_id=tenant_id,
            community_id=community_id,
            provider=data.provider,
            api_key=data.api_key,
            user_id=user_id,
        )
    except ApiError as exc:
        logger.warning("%s refused: %s: %s", request.path, type(exc).__name__, exc.message)
        return _err(exc)
    return _key_dto(info)


@media_byok_admin_bp.route("/<int:community_id>/media/byok-keys/<provider>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MediaMessageResponse)
async def delete_media_byok_key(
    community_id: int, provider: str
) -> MediaMessageResponse | tuple[dict[str, object], int]:
    """`DELETE .../media/byok-keys/<provider>` -- deactivates; allowed at any tier."""
    async_dal, dal = _dal()
    try:
        tenant_id, _ = await _require_admin(community_id)
        await media.delete_media_key(
            async_dal, dal, tenant_id=tenant_id, community_id=community_id, provider=provider
        )
    except ApiError as exc:
        logger.warning("%s refused: %s: %s", request.path, type(exc).__name__, exc.message)
        return _err(exc)
    return MediaMessageResponse(success=True, message=f"{provider} key deactivated")


BLUEPRINTS = [media_byok_admin_bp]
