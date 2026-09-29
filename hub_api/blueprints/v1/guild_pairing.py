"""v1 `guild-pairings` group -- multi-tenant Discord guild<->community pairing/binding/role API.

(#500/#501)

Two blueprints, mounted separately, because the two surfaces have different
actors and authorization models (schema contract:
`docs/superpowers/specs/2026-09-29-guild-binding-contract.md`):

- `guild_pairing_bp` (`/api/v1/tenant/guild-pairings/...`) -- tenant-admin
  self-service over the caller's OWN tenant's rows. `tenant_middleware`
  first, then `tenant:read`/`tenant:admin` scopes, same ladder
  `blueprints/v1/ingest_sources.py` uses.
- `guild_authority_bp` (`/api/v1/guild-authority/...`) -- cross-tenant
  actions gated by Discord guild-authority (`GuildAuthorityVerifier`,
  `services/guild_pairing_authority_service.py`), not a tenant scope bundle.
  `tenant_middleware` still runs first (every JWT carries a `tenant` claim,
  security.md) to validate the bearer token, but the actual admission
  decision is the injected verifier -- a caller may legitimately act on a
  guild paired with a tenant they have no role in at all. `require_scope`
  is layered on top as defense-in-depth using `guild.authority:read`/
  `guild.authority:write`, explicit entries in the `global` level of
  `libs/flask_core/flask_core/auth.py`'s `SCOPE_BUNDLES` (admin: both,
  maintainer/viewer: read only) -- `GuildAuthorityVerifier` remains the
  real, live-Discord-permission gate; this scope only proves the caller is
  an authenticated platform user in the first place.

Feature-flagged behind `waddles.guild-pairing` (default OFF) -- every route
below 404s while the flag is off, per this task's own "Security and flags"
requirement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from pydantic import ConfigDict
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import guild_pairing_authority_service as authority_svc
from services import guild_pairing_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError

logger = logging.getLogger(__name__)

#: PostHog flag key (waddles.md's `{product}.{feature-name}` convention),
#: default OFF until validated -- see module docstring.
FEATURE_GUILD_PAIRING = "waddles.guild-pairing"

guild_pairing_bp = Blueprint(
    "v1_guild_pairing", __name__, url_prefix="/api/v1/tenant/guild-pairings"
)
guild_authority_bp = Blueprint("v1_guild_authority", __name__, url_prefix="/api/v1/guild-authority")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _verifier() -> authority_svc.GuildAuthorityVerifier | None:
    """The injected `GuildAuthorityVerifier`, or `None` if not wired (fails closed downstream)."""
    return cast(
        "authority_svc.GuildAuthorityVerifier | None",
        current_app.config.get("guild_authority_verifier"),
    )


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _tenant_id() -> int:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware always runs first
    return cast(int, ctx.tenant_id)


async def _flag_enabled() -> bool:
    ctx = get_tenant_context(request)
    tenant_slug = ctx.tenant_slug if ctx is not None else "global"
    return cast(bool, await feature_enabled(FEATURE_GUILD_PAIRING, tenant=tenant_slug))


_FLAG_DISABLED_RESPONSE: tuple[dict[str, object], int] = (
    {"success": False, "error": {"code": "NOT_FOUND", "message": "Not found"}},
    404,
)


# --------------------------------------------------------------------------
# Response/request DTOs
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PairingDTO:
    """One `guild_tenant_pairings` row on the wire."""

    id: str
    platform: str
    guildId: str
    status: str
    consentAt: str | None
    lastVerifiedAt: str | None
    revokedAt: str | None
    revokedBy: str | None
    createdAt: str


@dataclass(slots=True, frozen=True)
class PairingListResponse:
    """Response DTO for `GET /api/v1/tenant/guild-pairings`."""

    success: bool
    pairings: list[PairingDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class BindingDTO:
    """One `community_channel_bindings` row on the wire."""

    id: str
    platform: str
    guildId: str
    channelId: str | None
    communityId: int
    pairingId: str
    status: str
    createdAt: str


@dataclass(slots=True, frozen=True)
class BindingResponse:
    """Response DTO for a single-binding result (create)."""

    success: bool
    binding: BindingDTO


@dataclass(slots=True, frozen=True)
class CreateBindingRequest:
    """`channelId=None` binds the guild default; set it to bind a specific channel."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    communityId: int
    channelId: str | None = None


@dataclass(slots=True, frozen=True)
class ManagedRoleDTO:
    """One `managed_roles` row -- `approvalStatus` distinguishes pending/approved/rejected."""

    id: str
    platform: str
    guildId: str
    roleId: str
    owningCommunityId: int
    registeredVia: str
    status: str
    approvalStatus: str
    approvedByUserId: int | None
    createdAt: str


@dataclass(slots=True, frozen=True)
class ManagedRoleListResponse:
    """Response DTO for `GET /api/v1/tenant/guild-pairings/roles`."""

    success: bool
    managedRoles: list[ManagedRoleDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ManagedRoleResponse:
    """Response DTO for role registration -- `approvalStatus` is `pending` for adopted."""

    success: bool
    managedRole: ManagedRoleDTO


@dataclass(slots=True, frozen=True)
class RequestRoleRegistrationRequest:
    """Request DTO for `POST .../roles` -- `registeredVia` is `created` or `adopted`."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    communityId: int
    roleId: str
    registeredVia: str


def _pairing_to_dto(row: Any) -> PairingDTO:
    return PairingDTO(
        id=str(row.id),
        platform=row.platform,
        guildId=row.guild_id,
        status=row.status,
        consentAt=row.consent_at.isoformat() if row.consent_at else None,
        lastVerifiedAt=row.last_verified_at.isoformat() if row.last_verified_at else None,
        revokedAt=row.revoked_at.isoformat() if row.revoked_at else None,
        revokedBy=row.revoked_by,
        createdAt=row.created_at.isoformat() if row.created_at else "",
    )


def _binding_to_dto(row: Any) -> BindingDTO:
    return BindingDTO(
        id=str(row.id),
        platform=row.platform,
        guildId=row.guild_id,
        channelId=row.channel_id,
        communityId=row.community_id,
        pairingId=str(row.pairing_id),
        status=row.status,
        createdAt=row.created_at.isoformat() if row.created_at else "",
    )


def _managed_role_to_dto(row: Any) -> ManagedRoleDTO:
    return ManagedRoleDTO(
        id=str(row.id),
        platform=row.platform,
        guildId=row.guild_id,
        roleId=row.role_id,
        owningCommunityId=row.owning_community_id,
        registeredVia=row.registered_via,
        status=row.status,
        approvalStatus=row.approval_status,
        approvedByUserId=row.approved_by_user_id,
        createdAt=row.created_at.isoformat() if row.created_at else "",
    )


# --------------------------------------------------------------------------
# Tenant-admin routes
# --------------------------------------------------------------------------


@guild_pairing_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:read")  # type: ignore[untyped-decorator]
@validate_response(PairingListResponse)
async def list_pairings() -> PairingListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/tenant/guild-pairings` -- the caller's own tenant's pairings."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    rows = await svc.list_pairings(_install_dal(), tenant_id=_tenant_id())
    return PairingListResponse(success=True, pairings=[_pairing_to_dto(r) for r in rows])


@guild_pairing_bp.route("/<string:pairing_id>/bindings", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(CreateBindingRequest)
@validate_response(BindingResponse, status_code=201)
async def create_binding(
    data: CreateBindingRequest, pairing_id: str
) -> tuple[BindingResponse | dict[str, object], int]:
    """`POST /api/v1/tenant/guild-pairings/<id>/bindings` -- bind a channel or the guild default."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    try:
        row = await svc.create_binding(
            _install_dal(),
            tenant_id=_tenant_id(),
            pairing_id=pairing_id,
            community_id=data.communityId,
            channel_id=data.channelId,
            created_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return BindingResponse(success=True, binding=_binding_to_dto(row)), 201


@guild_pairing_bp.route("/<string:pairing_id>/bindings/<string:binding_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
async def delete_binding(pairing_id: str, binding_id: str) -> tuple[Any, int]:
    """`DELETE /api/v1/tenant/guild-pairings/<id>/bindings/<bindingId>` -- unbind (soft-revoke)."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    deleted = await svc.unbind(
        _install_dal(),
        tenant_id=_tenant_id(),
        pairing_id=pairing_id,
        binding_id=binding_id,
        actor_id=get_current_user_id(request),
    )
    if not deleted:
        return _err(ApiError("Binding not found", 404, "NOT_FOUND"))
    return "", 204


@guild_pairing_bp.route("/<string:pairing_id>/roles", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(RequestRoleRegistrationRequest)
@validate_response(ManagedRoleResponse, status_code=201)
async def request_role_registration(
    data: RequestRoleRegistrationRequest, pairing_id: str
) -> tuple[ManagedRoleResponse | dict[str, object], int]:
    """`POST /api/v1/tenant/guild-pairings/<id>/roles` -- register/request-adoption of a role."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    try:
        row = await svc.request_role_registration(
            _install_dal(),
            tenant_id=_tenant_id(),
            pairing_id=pairing_id,
            community_id=data.communityId,
            role_id=data.roleId,
            registered_via=data.registeredVia,
            requested_by=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return ManagedRoleResponse(success=True, managedRole=_managed_role_to_dto(row)), 201


@guild_pairing_bp.route("/roles", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:read")  # type: ignore[untyped-decorator]
@validate_response(ManagedRoleListResponse)
async def list_managed_roles() -> ManagedRoleListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/tenant/guild-pairings/roles` -- the caller's own tenant's managed roles."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    rows = await svc.list_managed_roles(_install_dal(), tenant_id=_tenant_id())
    return ManagedRoleListResponse(
        success=True, managedRoles=[_managed_role_to_dto(r) for r in rows]
    )


# --------------------------------------------------------------------------
# Guild-authority routes (cross-tenant, GuildAuthorityVerifier-gated)
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ApproveAdoptedRoleResponse:
    """Response DTO for the approve-adopted-role route."""

    success: bool
    managedRole: ManagedRoleDTO


@dataclass(slots=True, frozen=True)
class RejectAdoptedRoleResponse:
    """Response DTO for the reject-adopted-role route."""

    success: bool
    managedRole: ManagedRoleDTO


@dataclass(slots=True, frozen=True)
class RevokePairingResponse:
    """Response DTO for the guild-authority pairing-revoke route."""

    success: bool
    pairing: PairingDTO


@dataclass(slots=True, frozen=True)
class GuildOverviewEntryDTO:
    """One paired-tenant summary row on the wire -- NO member data."""

    pairingId: str
    tenantId: int
    status: str
    boundCommunityIds: list[int]
    ownedRoleIds: list[str]


@dataclass(slots=True, frozen=True)
class GuildOverviewResponse:
    """Response DTO for the guild-authority overview route."""

    success: bool
    entries: list[GuildOverviewEntryDTO] = field(default_factory=list)


@guild_authority_bp.route("/managed-roles/<string:managed_role_id>/approve", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild.authority:write")  # type: ignore[untyped-decorator]
@validate_response(ApproveAdoptedRoleResponse, status_code=201)
async def approve_adopted_role(
    managed_role_id: str,
) -> tuple[ApproveAdoptedRoleResponse | dict[str, object], int]:
    """`POST /api/v1/guild-authority/managed-roles/<managedRoleId>/approve`."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    try:
        row = await authority_svc.approve_adopted_role(
            _install_dal(),
            _verifier(),
            managed_role_id=managed_role_id,
            approver_hub_user_id=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return ApproveAdoptedRoleResponse(success=True, managedRole=_managed_role_to_dto(row)), 201


@guild_authority_bp.route("/managed-roles/<string:managed_role_id>/reject", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild.authority:write")  # type: ignore[untyped-decorator]
@validate_response(RejectAdoptedRoleResponse, status_code=201)
async def reject_adopted_role(
    managed_role_id: str,
) -> tuple[RejectAdoptedRoleResponse | dict[str, object], int]:
    """`POST /api/v1/guild-authority/managed-roles/<managedRoleId>/reject`."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    try:
        row = await authority_svc.reject_adopted_role(
            _install_dal(),
            _verifier(),
            managed_role_id=managed_role_id,
            rejecter_hub_user_id=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return RejectAdoptedRoleResponse(success=True, managedRole=_managed_role_to_dto(row)), 201


@guild_authority_bp.route("/<string:pairing_id>/revoke", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild.authority:write")  # type: ignore[untyped-decorator]
@validate_response(RevokePairingResponse)
async def revoke_pairing(
    pairing_id: str,
) -> RevokePairingResponse | tuple[dict[str, object], int]:
    """`POST /api/v1/guild-authority/<pairingId>/revoke` -- guild-side revocation cascade."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    try:
        row = await authority_svc.revoke_pairing(
            _install_dal(),
            _verifier(),
            pairing_id=pairing_id,
            revoker_hub_user_id=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return RevokePairingResponse(success=True, pairing=_pairing_to_dto(row))


@guild_authority_bp.route("/<string:platform>/<string:guild_id>/overview", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild.authority:read")  # type: ignore[untyped-decorator]
@validate_response(GuildOverviewResponse)
async def guild_overview(
    platform: str, guild_id: str
) -> GuildOverviewResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/guild-authority/<platform>/<guildId>/overview` -- NO member data."""
    if not await _flag_enabled():
        return _FLAG_DISABLED_RESPONSE
    try:
        entries = await authority_svc.guild_overview(
            _install_dal(),
            _verifier(),
            platform=platform,
            guild_id=guild_id,
            requester_hub_user_id=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return GuildOverviewResponse(
        success=True,
        entries=[
            GuildOverviewEntryDTO(
                pairingId=e.pairingId,
                tenantId=e.tenantId,
                status=e.status,
                boundCommunityIds=e.boundCommunityIds,
                ownedRoleIds=e.ownedRoleIds,
            )
            for e in entries
        ],
    )


BLUEPRINTS: list[Blueprint] = [guild_pairing_bp, guild_authority_bp]
