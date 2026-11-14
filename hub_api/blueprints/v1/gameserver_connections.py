"""Game-server connection gateway: create / test / delete a community's registered game server.

A thin, purpose-built front for the ``gameserver`` provider of the shared provider-connection
framework (``services/provider_connections/gameserver.py``). The admin supplies ``host``, ``port`` and
``rcon_password``; they are sealed together at rest (AES-256-GCM, row-bound AAD) and are WRITE-ONLY:
no route ever returns them, and every response carries only the opaque ``serverId`` UUID a bundle
names the server by. The data plane's ``gameserver-net`` host capability is the only reader.

Authorization is the framework's: ``connections:admin`` scope, DB-backed "administers THIS community
of THEIR tenant" check, then the feature gate -- the tenant never comes from the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from pydantic import ConfigDict
from quart import Blueprint
from quart_schema import validate_request, validate_response

from blueprints.v1.provider_connections import (
    SCOPE_CONNECTIONS_ADMIN,
    _admin_guard,
    _guarded,
    _service,
)
from services.provider_connections import errors as pc_errors
from services.provider_connections.gameserver import (
    PROVIDER_ID,
    build_sealed_target,
    parse_sealed_target,
)
from services.provider_connections.gameserver_probe import probe

logger = logging.getLogger(__name__)

gameserver_connections_bp = Blueprint("v1_gameserver_connections", __name__, url_prefix="/api/v1")

_BASE = "/communities/<int:community_id>/gameserver-connections"
_ONE = _BASE + "/<int:connection_id>"


@dataclass(slots=True, frozen=True)
class CreateGameServerRequest:
    """Body of ``POST``. ``host``/``port``/``rconPassword`` are write-only and never echoed."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    host: str
    port: int
    rconPassword: str = ""
    game: str | None = None
    label: str | None = None
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class GameServerDTO:
    """What an admin may see of a registered server: ids and display fields only."""

    connectionId: int
    serverId: str
    game: str | None
    label: str | None
    enabled: bool
    usable: bool


@dataclass(slots=True, frozen=True)
class GameServerResponse:
    """Envelope for one server."""

    success: bool
    server: GameServerDTO


@dataclass(slots=True, frozen=True)
class GameServerTestResponse:
    """Closed result code of the connectivity test; never an address or secret."""

    success: bool
    result: str


@dataclass(slots=True, frozen=True)
class GameServerDeleteResponse:
    """Envelope for delete."""

    success: bool
    purged: bool


def _dto(view: object) -> GameServerDTO:
    """Project a framework ``ConnectionView`` to the admin-visible fields."""
    binding = view.binding  # type: ignore[attr-defined]
    meta = view.public_metadata  # type: ignore[attr-defined]
    return GameServerDTO(
        connectionId=binding.connection.id,
        serverId=str(meta.get("server_id", "")),
        game=meta.get("game"),
        label=meta.get("label"),
        enabled=binding.grant.enabled,
        usable=view.usable,  # type: ignore[attr-defined]
    )


@gameserver_connections_bp.route(_BASE, methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[misc]
@validate_request(CreateGameServerRequest)
@validate_response(GameServerResponse, 201)
@_guarded("gameserver_create")
async def create_gameserver(
    community_id: int, data: CreateGameServerRequest
) -> tuple[GameServerResponse, int]:
    """``POST`` -- validate, mint the server id, seal host|port|password, store."""
    tenant_id, actor = await _admin_guard(community_id)
    token = build_sealed_target(data.host, data.port, data.rconPassword)
    metadata: dict[str, str] = {"server_id": str(uuid4())}
    if data.game:
        metadata["game"] = data.game
    if data.label:
        metadata["label"] = data.label
    view = await _service().create(
        tenant_id=tenant_id,
        community_id=community_id,
        actor_user_id=actor,
        provider=PROVIDER_ID,
        access_token=token,
        refresh_token=None,
        metadata=metadata,
        enabled=data.enabled,
    )
    logger.info("gameserver.created community_id=%d", community_id)
    return GameServerResponse(success=True, server=_dto(view)), 201


@gameserver_connections_bp.route(_ONE + "/test", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[misc]
@validate_response(GameServerTestResponse)
@_guarded("gameserver_test")
async def test_gameserver(community_id: int, connection_id: int) -> GameServerTestResponse:
    """``POST .../test`` -- pinned, guarded RCON login (or TCP connect); returns a result code."""
    tenant_id, _actor = await _admin_guard(community_id)
    service = _service()
    # Ownership first: the connection must be THIS tenant's, in THIS community (404 otherwise).
    view = await service.get(
        tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
    )
    if view.binding.connection.platform != PROVIDER_ID:
        raise pc_errors.ConnectionNotFound("connection not found")
    resolved = await service.resolve(community_id=community_id, connection_id=connection_id)
    if resolved.access_token is None:
        raise pc_errors.ConnectionNotUsable("the connection has no credential")
    host, port, password = parse_sealed_target(resolved.access_token)
    result = await probe(host, port, password)
    logger.info("gameserver.tested community_id=%d result=%s", community_id, result)
    return GameServerTestResponse(success=True, result=result)


@gameserver_connections_bp.route(_ONE, methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[misc]
@validate_response(GameServerDeleteResponse)
@_guarded("gameserver_delete")
async def delete_gameserver(community_id: int, connection_id: int) -> GameServerDeleteResponse:
    """``DELETE`` -- remove the server; the sealed secret is purged with the last link."""
    tenant_id, _actor = await _admin_guard(community_id)
    service = _service()
    view = await service.get(
        tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
    )
    if view.binding.connection.platform != PROVIDER_ID:
        raise pc_errors.ConnectionNotFound("connection not found")
    purged = await service.delete(
        tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
    )
    return GameServerDeleteResponse(success=True, purged=purged)


BLUEPRINTS = [gameserver_connections_bp]
