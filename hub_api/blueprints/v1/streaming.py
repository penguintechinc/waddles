"""v1 `streaming` group -- stream key + restream destination admin, native to hub-api.

Mounted at `/api/v1/admin/<community_id>/streams/*` (paths unchanged from the legacy Node
`streamingController.js` and `frontend/src/services/api.js`). This used to be a reverse proxy to
the standalone `video_proxy_module`, which stored stream keys in plaintext and returned them on
every GET; it is now backed by svc-streaming's `streaming_configs` + the 0076
`streaming_destinations` table via `services.stream_keys_service` (hash-only inbound key,
AES-GCM-sealed destination credentials -- see that module and alembic 0076).

Contract highlights:

- The plaintext stream key appears ONLY in the `streamKey` field of the `POST .../streams` (create)
  and `POST .../streams/key/regenerate` responses, with `Cache-Control: no-store`; no GET ever
  returns it (the config DTO carries `stream_key_set` + a non-secret `stream_key_fingerprint`).
- Auth: community admin via `services.community_authz.authorize_community(admin=True)` (tenant
  middleware first). Destination management is additionally gated on the Professional-tier
  `streaming.broadcast` feature (forwarding), as before.
- Every response is an explicit dataclass DTO (`@validate_response`), never a raw row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import stream_keys_service as svc
from services.community_authz import authorize_community
from services.errors import ApiError, bad_request, payment_required
from services.schema import bind_stream_keys_tables
from services.stream_keys_service import (
    StreamConfigDTO,
    StreamDestinationDTO,
    StreamStatusDTO,
)
from services.url_guard import validate_outbound_url

streaming_bp = Blueprint("v1_streaming", __name__, url_prefix="/api/v1/admin")

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s `streaming.broadcast`
#: Feature contract, Professional tier (forward/record/transcode destination management).
FEATURE_STREAMING_BROADCAST = "waddles.streaming.broadcast"

#: Never let a browser/proxy cache a response that carries a one-time secret.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@dataclass(slots=True, frozen=True)
class ConfigResponse:
    """Response DTO for `GET .../streams` -- `config` is null until one is created."""

    success: bool
    config: StreamConfigDTO | None


@dataclass(slots=True, frozen=True)
class KeyRevealResponse:
    """Response DTO for create/regenerate -- the ONLY place a plaintext stream key is returned."""

    success: bool
    config: StreamConfigDTO
    streamKey: str  # noqa: N815 - wire name pinned to the legacy `{streamKey}` contract


@dataclass(slots=True, frozen=True)
class DestinationsResponse:
    """Response DTO for `GET .../streams/destinations`."""

    success: bool
    destinations: list[StreamDestinationDTO]


@dataclass(slots=True, frozen=True)
class DestinationResponse:
    """Response DTO for destination create / force-cut."""

    success: bool
    destination: StreamDestinationDTO


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for destination delete."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class StatusResponse:
    """Response DTO for `GET .../streams/status`."""

    success: bool
    status: StreamStatusDTO


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config with this group's tables bound."""
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    bind_stream_keys_tables(dal)
    return async_dal, dal


async def _authorize(community_id: int, *, require_broadcast: bool = False) -> tuple[Any, Any]:
    """Tenant + community-admin gate (and optionally the broadcast flag); returns the DAL pair."""
    async_dal, dal = _dal()
    await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
    if require_broadcast:
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
        if not await feature_enabled(FEATURE_STREAMING_BROADCAST, tenant=ctx.tenant_slug):
            raise payment_required("Broadcast forwarding requires a Professional plan or higher")
    return async_dal, dal


def _ingest_url() -> str | None:
    """Public RTMP ingest base URL (operator-set; the key is NOT part of it)."""
    return os.getenv("STREAMING_INGEST_RTMP_URL") or None


async def _json_body() -> dict[str, Any]:
    body = await request.get_json(force=True, silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise bad_request("Request body must be a JSON object")
    return body


@streaming_bp.route("/<int:community_id>/streams", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(ConfigResponse)
async def get_stream_config(
    community_id: int,
) -> ConfigResponse | tuple[dict[str, object], int]:
    """Get the stream configuration (never the key) for a community."""
    try:
        async_dal, dal = await _authorize(community_id)
        config = await svc.get_config(
            async_dal, dal, community_id=community_id, ingest_url=_ingest_url()
        )
    except ApiError as exc:
        return _err(exc)
    return ConfigResponse(success=True, config=config)


@streaming_bp.route("/<int:community_id>/streams", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(KeyRevealResponse, 201)
async def create_stream_config(
    community_id: int,
) -> tuple[KeyRevealResponse, int, dict[str, str]] | tuple[dict[str, object], int]:
    """Create the community's stream config; the one-time plaintext key is in `streamKey`."""
    try:
        async_dal, dal = await _authorize(community_id)
        body = await _json_body()
        config, key = await svc.create_config(
            async_dal,
            dal,
            community_id=community_id,
            enabled=body.get("enabled") is not False,
            ingest_url=_ingest_url(),
        )
    except ApiError as exc:
        return _err(exc)
    return KeyRevealResponse(success=True, config=config, streamKey=key), 201, _NO_STORE


@streaming_bp.route("/<int:community_id>/streams/key/regenerate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(KeyRevealResponse)
async def regenerate_stream_key(
    community_id: int,
) -> tuple[KeyRevealResponse, int, dict[str, str]] | tuple[dict[str, object], int]:
    """Rotate the stream key; the old key stops authorizing immediately."""
    try:
        async_dal, dal = await _authorize(community_id)
        config, key = await svc.rotate_key(
            async_dal, dal, community_id=community_id, ingest_url=_ingest_url()
        )
    except ApiError as exc:
        return _err(exc)
    return KeyRevealResponse(success=True, config=config, streamKey=key), 200, _NO_STORE


@streaming_bp.route("/<int:community_id>/streams/destinations", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(DestinationsResponse)
async def get_destinations(
    community_id: int,
) -> DestinationsResponse | tuple[dict[str, object], int]:
    """List restream destinations (URL redacted to scheme://host, key never returned)."""
    try:
        async_dal, dal = await _authorize(community_id, require_broadcast=True)
        destinations = await svc.list_destinations(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return DestinationsResponse(success=True, destinations=destinations)


@streaming_bp.route("/<int:community_id>/streams/destinations", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(DestinationResponse, 201)
async def add_destination(
    community_id: int,
) -> tuple[DestinationResponse, int] | tuple[dict[str, object], int]:
    """Add a restream destination; its URL and key are sealed at rest."""
    try:
        async_dal, dal = await _authorize(community_id, require_broadcast=True)
        data = svc.validate_destination_input(await _json_body())
        # SSRF guard: runs in this frame, before anything is stored for svc-streaming to dial.
        await validate_outbound_url(data.rtmp_url, allowed_schemes=("rtmp", "rtmps"))
        destination = await svc.add_destination(
            async_dal, dal, community_id=community_id, data=data
        )
    except ApiError as exc:
        return _err(exc)
    return DestinationResponse(success=True, destination=destination), 201


@streaming_bp.route("/<int:community_id>/streams/destinations/<destination_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MessageResponse)
async def remove_destination(
    community_id: int, destination_id: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Remove a restream destination (404 if it is not this community's)."""
    try:
        async_dal, dal = await _authorize(community_id, require_broadcast=True)
        await svc.remove_destination(
            async_dal, dal, community_id=community_id, destination_id=destination_id
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Destination removed successfully")


@streaming_bp.route(
    "/<int:community_id>/streams/destinations/<destination_id>/force-cut", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@validate_response(DestinationResponse)
async def toggle_force_cut(
    community_id: int, destination_id: str
) -> DestinationResponse | tuple[dict[str, object], int]:
    """Set force-cut from a boolean body field, or toggle it when the body omits one."""
    try:
        async_dal, dal = await _authorize(community_id, require_broadcast=True)
        body = await _json_body()
        raw = body.get("force_cut", body.get("forceCut"))
        if raw is not None and not isinstance(raw, bool):
            raise bad_request("force_cut must be a boolean")
        destination = await svc.set_force_cut(
            async_dal,
            dal,
            community_id=community_id,
            destination_id=destination_id,
            force_cut=raw,
        )
    except ApiError as exc:
        return _err(exc)
    return DestinationResponse(success=True, destination=destination)


@streaming_bp.route("/<int:community_id>/streams/status", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(StatusResponse)
async def get_stream_status(
    community_id: int,
) -> StatusResponse | tuple[dict[str, object], int]:
    """Get live streaming status for a community."""
    try:
        async_dal, dal = await _authorize(community_id)
        status = await svc.get_status(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return StatusResponse(success=True, status=status)


BLUEPRINTS: list[Blueprint] = [streaming_bp]
