"""v1 `community.guild_pairing` group -- Bar Citizen guild<->tenant pairing + role-sync CRUD.

**Foundation REST surface** other Bar Citizen units (C/D's OAuth
bot-install flows, F's role-sync worker) sit behind -- `services/
guild_pairing.py` (the data-access/service layer) and `services/
credential_resolver.py` (the `CredentialResolver` seam) are the two
importable contracts this group defines; this module is their one HTTP
surface.

Tenant-admin-scoped CRUD, same `tenant_middleware` -> `require_scope`
chain and `community_in_tenant()` ownership gate every other
Community-module admin route in this port uses (see
`blueprints/v1/community_connections.py`'s own module docstring) -- a
guild may be paired with more than one tenant/community (N:M, migration
0034), but a caller only ever sees/edits pairings under a `community_id`
their own token's tenant owns.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py`/`blueprints/__init__.py`
needed.

Feature-gated the same two-gate way `community_connections.py`/
`community_activity.py` gate themselves: every route below calls
`feature_enabled(FEATURE_GUILD_PAIRING, tenant=ctx.tenant_slug)` before
touching the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import guild_pairing as pairing_svc
from services.community_common import community_in_tenant
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, not_found, payment_required

guild_pairing_bp = Blueprint("v1_guild_pairing", __name__, url_prefix="/api/v1")

#: Two-gate feature flag -- this port's `waddles.<module>.<feature>` convention
#: (`waddles.community.connections`, `waddles.community.activity`, ...).
FEATURE_GUILD_PAIRING = "waddles.community.guild_pairing"


# ---------------------------------------------------------------------------
# Response DTOs (quart-schema `@validate_response` targets -- security.md
# Output Validation: never a raw ORM object/dict serialized directly)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GuildPairingResponse:
    """Single-pairing response envelope."""

    success: bool
    pairing: pairing_svc.GuildPairing


@dataclass(slots=True, frozen=True)
class GuildPairingListResponse:
    """List-of-pairings response envelope."""

    success: bool
    pairings: list[pairing_svc.GuildPairing]


@dataclass(slots=True, frozen=True)
class RoleSyncBindingResponse:
    """Single-binding response envelope."""

    success: bool
    binding: pairing_svc.RoleSyncBinding


@dataclass(slots=True, frozen=True)
class RoleSyncBindingListResponse:
    """List-of-bindings response envelope."""

    success: bool
    bindings: list[pairing_svc.RoleSyncBinding]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dal() -> Any:
    return current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


async def _feature_gate_and_tenant_check(community_id: int) -> tuple[dict[str, object], int] | None:
    """Shared guard every route runs first: feature flag, then tenant ownership.

    Returns an error tuple to short-circuit the route on, or `None` to
    proceed.
    """
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_GUILD_PAIRING, tenant=ctx.tenant_slug):
        return _err(payment_required("Guild pairing requires a Professional plan or higher"))
    if not community_in_tenant(_dal(), community_id, ctx):
        return _err(not_found("Community not found"))
    return None


# ---------------------------------------------------------------------------
# Pairings: list / create
# ---------------------------------------------------------------------------


@guild_pairing_bp.route("/communities/<int:community_id>/guild-pairings", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:read")  # type: ignore[untyped-decorator]
@validate_response(GuildPairingListResponse)
async def list_pairings(
    community_id: int,
) -> GuildPairingListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/communities/<id>/guild-pairings`."""
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error
    pairings = pairing_svc.list_pairings(_dal(), community_id)
    return GuildPairingListResponse(success=True, pairings=pairings)


@guild_pairing_bp.route("/communities/<int:community_id>/guild-pairings", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:write")  # type: ignore[untyped-decorator]
@validate_response(GuildPairingResponse, status_code=201)
async def create_pairing(
    community_id: int,
) -> tuple[GuildPairingResponse, int] | tuple[dict[str, object], int]:
    """`POST /api/v1/communities/<id>/guild-pairings`."""
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error

    body = await request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return _err(bad_request("Request body must be a JSON object"))

    actor_user_id = get_current_user_id(request)
    try:
        pairing = pairing_svc.create_pairing(
            _dal(),
            community_id,
            discord_guild_id=str(body.get("discord_guild_id", "")),
            direction=str(body.get("direction", "")),
            role_name_prefix=str(body.get("role_name_prefix", "")),
            sync_enabled=bool(body.get("sync_enabled", False)),
            actor_user_id=actor_user_id,
        )
    except ApiError as exc:
        return _err(exc)
    return GuildPairingResponse(success=True, pairing=pairing), 201


# ---------------------------------------------------------------------------
# Pairings: update / delete
# ---------------------------------------------------------------------------


@guild_pairing_bp.route(
    "/communities/<int:community_id>/guild-pairings/<int:pairing_id>", methods=["PATCH"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:write")  # type: ignore[untyped-decorator]
@validate_response(GuildPairingResponse)
async def update_pairing(
    community_id: int, pairing_id: int
) -> GuildPairingResponse | tuple[dict[str, object], int]:
    """`PATCH /api/v1/communities/<id>/guild-pairings/<pairing_id>`.

    Accepts any subset of `sync_enabled` / `direction` / `role_name_prefix`.
    """
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error

    body = await request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return _err(bad_request("Request body must be a JSON object"))

    try:
        pairing = pairing_svc.update_pairing(
            _dal(),
            community_id,
            pairing_id,
            sync_enabled=body["sync_enabled"] if "sync_enabled" in body else None,
            direction=body.get("direction"),
            role_name_prefix=body.get("role_name_prefix"),
        )
    except ApiError as exc:
        return _err(exc)
    return GuildPairingResponse(success=True, pairing=pairing)


@guild_pairing_bp.route(
    "/communities/<int:community_id>/guild-pairings/<int:pairing_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:write")  # type: ignore[untyped-decorator]
async def delete_pairing(community_id: int, pairing_id: int) -> tuple[Any, int]:
    """`DELETE /api/v1/communities/<id>/guild-pairings/<pairing_id>`."""
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error

    deleted = pairing_svc.delete_pairing(_dal(), community_id, pairing_id)
    if not deleted:
        return _err(not_found("Pairing not found"))
    return "", 204


# ---------------------------------------------------------------------------
# Role-sync bindings: list / create / delete
# ---------------------------------------------------------------------------


@guild_pairing_bp.route(
    "/communities/<int:community_id>/guild-pairings/<int:pairing_id>/role-bindings",
    methods=["GET"],
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:read")  # type: ignore[untyped-decorator]
@validate_response(RoleSyncBindingListResponse)
async def list_bindings(
    community_id: int, pairing_id: int
) -> RoleSyncBindingListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/communities/<id>/guild-pairings/<pairing_id>/role-bindings`."""
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error

    try:
        bindings = pairing_svc.list_bindings(_dal(), community_id, pairing_id)
    except ApiError as exc:
        return _err(exc)
    return RoleSyncBindingListResponse(success=True, bindings=bindings)


@guild_pairing_bp.route(
    "/communities/<int:community_id>/guild-pairings/<int:pairing_id>/role-bindings",
    methods=["POST"],
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:write")  # type: ignore[untyped-decorator]
@validate_response(RoleSyncBindingResponse, status_code=201)
async def create_binding(
    community_id: int, pairing_id: int
) -> tuple[RoleSyncBindingResponse, int] | tuple[dict[str, object], int]:
    """`POST /api/v1/communities/<id>/guild-pairings/<pairing_id>/role-bindings`."""
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error

    body = await request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return _err(bad_request("Request body must be a JSON object"))

    try:
        binding = pairing_svc.create_binding(
            _dal(),
            community_id,
            pairing_id,
            sync_scope=str(body.get("sync_scope", "")),
            discord_role_id=str(body.get("discord_role_id", "")),
            subscriber_tier=body.get("subscriber_tier"),
            community_role=body.get("community_role"),
        )
    except ApiError as exc:
        return _err(exc)
    return RoleSyncBindingResponse(success=True, binding=binding), 201


@guild_pairing_bp.route(
    "/communities/<int:community_id>/guild-pairings/<int:pairing_id>/role-bindings/<int:binding_id>",
    methods=["DELETE"],
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.guild_pairing:write")  # type: ignore[untyped-decorator]
async def delete_binding(community_id: int, pairing_id: int, binding_id: int) -> tuple[Any, int]:
    """`DELETE /api/v1/communities/<id>/guild-pairings/<pairing_id>/role-bindings/<binding_id>`."""
    gate_error = await _feature_gate_and_tenant_check(community_id)
    if gate_error is not None:
        return gate_error

    try:
        deleted = pairing_svc.delete_binding(_dal(), community_id, pairing_id, binding_id)
    except ApiError as exc:
        return _err(exc)
    if not deleted:
        return _err(not_found("Binding not found"))
    return "", 204


BLUEPRINTS: list[Blueprint] = [guild_pairing_bp]
