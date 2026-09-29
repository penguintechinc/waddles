"""Tenant-admin operations over the multi-tenant guild<->community pairing schema (#500/#501).

Scope of this module (see `docs/superpowers/specs/2026-09-29-guild-binding-contract.md`
for the full schema contract): everything a **tenant admin** does against
their own tenant's rows in `guild_tenant_pairings`, `community_channel_bindings`,
and `managed_roles` -- list pairings, bind/unbind a channel or the guild
default to one of their own communities, and request role registration.
Cross-tenant guild-authority actions (approve an adopted-role request,
revoke a pairing from the guild side, the cross-tenant overview) live in
`services/guild_pairing_authority_service.py`, a deliberately separate
module since the two surfaces have different actors and different
authorization models (tenant scope bundle vs. Discord guild authority).

**Out of scope, by design (concurrent work on `feature/guild-pairing-oauth`).**
This module never touches `tenant_platform_credentials`, never initiates or
completes an OAuth2 bot-install callback, and never resolves Discord
credentials -- it only reads/writes the four pairing/binding/role tables
through `install_dal` (`services/bundle_install_dal.py`, penguin-dal
`AsyncDB`, R52 convention -- same table-access idiom `services/
ingest_source_registry_service.py` uses).

**Non-leaky conflict responses.** The DB enforces exclusivity with partial
unique indexes (`uq_channel_binding_exclusive`, `uq_guild_default_binding_exclusive`)
that span every tenant paired with a guild, not just the caller's own tenant
-- so a 409 here must never reveal which *other* tenant or community holds
the conflicting binding (`_CHANNEL_BINDING_CONFLICT_MESSAGE`/
`_GUILD_DEFAULT_CONFLICT_MESSAGE` are static, generic strings; the caught
`IntegrityError` is never echoed to the client).

**Adopted-role registration, a documented schema gap.** `managed_roles`'s own
`chk_managed_roles_adopted_approval` CHECK constraint requires
`approved_by_user_id IS NOT NULL` for any `registered_via='adopted'` row --
so an adopted registration literally cannot be inserted before guild-authority
approves it, and migration 0038 has no separate "pending role registration"
table to stage the request in the meantime. Rather than add a new migration
(out of this slice's scope), a pending adoption request is staged as an
`audit_log` row (`action='managed_role.adoption_requested'`) -- the audit
log's own `id` is the request handle guild-authority approval acts on (see
`guild_pairing_authority_service.approve_adopted_role`). This is a real,
called-out gap in the same "known gap, documented, not silently bridged"
style the migration's own docstring uses, not a defect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy.exc import IntegrityError

from services import bundle_audit
from services.bundle_telemetry import bundle_span
from services.errors import ApiError, bad_request, not_found, unprocessable

#: Same platform vocabulary as migration 0038's `platform VARCHAR(50) DEFAULT 'discord'`
#: columns -- only Discord is modeled by this schema today.
SUPPORTED_PLATFORMS = frozenset({"discord"})

_ROLE_REGISTRATION_KINDS = frozenset({"created", "adopted"})

#: Conservative bounds/shape check on externally-sourced Discord snowflake ids
#: (guild/channel/role) -- defense-in-depth, not the injection defense itself
#: (penguin-dal parameterizes every value below). Mirrors `ingest_source_
#: registry_service._SOURCE_ID_RE`'s own rationale.
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")

#: Static, non-leaky conflict messages -- see module docstring. Never
#: interpolate the conflicting tenant/community into either message.
_CHANNEL_BINDING_CONFLICT_MESSAGE = (
    "This channel is already bound to a community. Unbind it before retrying."
)
_GUILD_DEFAULT_CONFLICT_MESSAGE = (
    "This guild already has a default community binding. Remove it before retrying."
)
_ROLE_OWNED_CONFLICT_MESSAGE = "This role is already registered to a community in this guild."


def _validate_snowflake(value: str, *, field_name: str) -> None:
    if not _SNOWFLAKE_RE.match(value):
        raise unprocessable(f"{field_name} must be a numeric Discord snowflake id")


async def _get_active_pairing_for_tenant(
    install_dal: AsyncDB, *, pairing_id: str, tenant_id: int
) -> Any:
    """The caller's own `guild_tenant_pairings` row, active, or a masking 404/422.

    404 if the pairing doesn't exist or belongs to a different tenant (never
    leak cross-tenant existence); 422 if it exists but isn't `active` yet --
    same "a binding requires an active pairing" rule the DB trigger
    (`trg_require_active_pairing_for_binding`) enforces, checked here first
    for a clear error message instead of a raw `IntegrityError`.
    """
    rows = await install_dal(
        (install_dal.guild_tenant_pairings.id == pairing_id)
        & (install_dal.guild_tenant_pairings.tenant_id == tenant_id)
    ).select()
    row = rows.first()
    if row is None:
        raise not_found("Guild pairing not found")
    if row.status != "active":
        raise unprocessable(
            f"Guild pairing is not active (status={row.status!r}) -- bindings and role "
            "registrations require an active pairing"
        )
    return row


async def _validate_community_tenant(
    install_dal: AsyncDB, *, community_id: int, tenant_id: int
) -> None:
    """Refuse a `community_id` that does not exist or belongs to a different tenant.

    404 deliberately masks cross-tenant existence -- same IDOR-masking
    rationale as `ingest_source_registry_service._validate_community_tenant`.
    """
    rows = await install_dal(
        (install_dal.communities.id == community_id)
        & (install_dal.communities.tenant_id == tenant_id)
    ).select()
    if rows.first() is None:
        raise not_found("Community not found")


@dataclass(slots=True, frozen=True)
class PendingRoleRegistration:
    """An `adopted` role registration awaiting guild-authority approval.

    `requestId` is the backing `audit_log.id` -- the handle
    `guild_pairing_authority_service.approve_adopted_role` consumes.
    """

    requestId: int
    platform: str
    guildId: str
    roleId: str
    communityId: int
    pairingId: str
    requestedAt: str


async def list_pairings(install_dal: AsyncDB, *, tenant_id: int) -> list[Any]:
    """Every `guild_tenant_pairings` row owned by the caller's own tenant."""
    rows = await install_dal(install_dal.guild_tenant_pairings.tenant_id == tenant_id).select(
        orderby=install_dal.guild_tenant_pairings.created_at
    )
    return list(rows)


async def list_managed_roles(install_dal: AsyncDB, *, tenant_id: int) -> list[Any]:
    """Every `managed_roles` row owned by the caller's own tenant (active or pending_cleanup)."""
    rows = await install_dal(install_dal.managed_roles.tenant_id == tenant_id).select(
        orderby=install_dal.managed_roles.created_at
    )
    return list(rows)


async def create_binding(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    pairing_id: str,
    community_id: int,
    channel_id: str | None,
    created_by: int | None,
) -> Any:
    """Bind a channel (`channel_id` set) or the guild default (`channel_id=None`) to a community.

    Exclusivity across every tenant paired with the guild is the DB's own
    partial unique indexes (module docstring) -- an `IntegrityError` here is
    mapped to a static, non-leaky 409, never the raw constraint text.
    """
    async with bundle_span(
        "hub.guild_pairing.create_binding", tenant_id=tenant_id, pairing_id=pairing_id
    ):
        if channel_id is not None:
            _validate_snowflake(channel_id, field_name="channelId")
        pairing = await _get_active_pairing_for_tenant(
            install_dal, pairing_id=pairing_id, tenant_id=tenant_id
        )
        await _validate_community_tenant(
            install_dal, community_id=community_id, tenant_id=tenant_id
        )

        now = datetime.now(UTC)
        try:
            new_id = await install_dal.community_channel_bindings.async_insert(
                platform=pairing.platform,
                guild_id=pairing.guild_id,
                channel_id=channel_id,
                community_id=community_id,
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                status="active",
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        except IntegrityError as exc:
            message = (
                _CHANNEL_BINDING_CONFLICT_MESSAGE
                if channel_id is not None
                else _GUILD_DEFAULT_CONFLICT_MESSAGE
            )
            raise ApiError(message, 409, "BINDING_CONFLICT") from exc

        await bundle_audit.record(
            install_dal,
            actor_id=created_by,
            action="guild_pairing.binding_created",
            target_type="community_channel_binding",
            target_id=str(new_id),
            details={
                "tenant_id": tenant_id,
                "pairing_id": pairing_id,
                "community_id": community_id,
            },
        )
        row = (
            await install_dal(install_dal.community_channel_bindings.id == new_id).select()
        ).first()
        return row


async def unbind(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    pairing_id: str,
    binding_id: str,
    actor_id: int | None,
) -> bool:
    """Revoke a `community_channel_bindings` row. `False` if absent/wrong tenant/pairing."""
    async with bundle_span("hub.guild_pairing.unbind", tenant_id=tenant_id, binding_id=binding_id):
        rows = await install_dal(
            (install_dal.community_channel_bindings.id == binding_id)
            & (install_dal.community_channel_bindings.tenant_id == tenant_id)
            & (install_dal.community_channel_bindings.pairing_id == pairing_id)
        ).select()
        row = rows.first()
        if row is None:
            return False
        await install_dal(install_dal.community_channel_bindings.id == binding_id).update(
            status="revoked", updated_at=datetime.now(UTC)
        )
        await bundle_audit.record(
            install_dal,
            actor_id=actor_id,
            action="guild_pairing.binding_revoked",
            target_type="community_channel_binding",
            target_id=str(binding_id),
            details={"tenant_id": tenant_id, "pairing_id": pairing_id},
        )
        return True


async def request_role_registration(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    pairing_id: str,
    community_id: int,
    role_id: str,
    registered_via: str,
    requested_by: int | None,
) -> Any:
    """Register (`created`) or request adoption (`adopted`) of a Discord role for a community.

    `created` rows are inserted immediately, active, no approval needed.
    `adopted` rows are NOT inserted into `managed_roles` yet -- see module
    docstring's "documented schema gap" -- a `PendingRoleRegistration` is
    returned instead, backed by an `audit_log` row.
    """
    async with bundle_span(
        "hub.guild_pairing.request_role_registration", tenant_id=tenant_id, role_id=role_id
    ):
        if registered_via not in _ROLE_REGISTRATION_KINDS:
            raise bad_request(f"registeredVia must be one of {sorted(_ROLE_REGISTRATION_KINDS)}")
        _validate_snowflake(role_id, field_name="roleId")
        pairing = await _get_active_pairing_for_tenant(
            install_dal, pairing_id=pairing_id, tenant_id=tenant_id
        )
        await _validate_community_tenant(
            install_dal, community_id=community_id, tenant_id=tenant_id
        )

        existing = await install_dal(
            (install_dal.managed_roles.platform == pairing.platform)
            & (install_dal.managed_roles.guild_id == pairing.guild_id)
            & (install_dal.managed_roles.role_id == role_id)
            & (install_dal.managed_roles.status == "active")
        ).select()
        if existing.first() is not None:
            raise ApiError(_ROLE_OWNED_CONFLICT_MESSAGE, 409, "ROLE_ALREADY_OWNED")

        now = datetime.now(UTC)
        if registered_via == "created":
            try:
                new_id = await install_dal.managed_roles.async_insert(
                    platform=pairing.platform,
                    guild_id=pairing.guild_id,
                    role_id=role_id,
                    tenant_id=tenant_id,
                    pairing_id=pairing_id,
                    owning_community_id=community_id,
                    registered_via="created",
                    approved_by_user_id=None,
                    approved_at=None,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            except IntegrityError as exc:
                raise ApiError(_ROLE_OWNED_CONFLICT_MESSAGE, 409, "ROLE_ALREADY_OWNED") from exc
            await bundle_audit.record(
                install_dal,
                actor_id=requested_by,
                action="guild_pairing.role_created",
                target_type="managed_role",
                target_id=str(new_id),
                details={"tenant_id": tenant_id, "community_id": community_id, "role_id": role_id},
            )
            return (await install_dal(install_dal.managed_roles.id == new_id).select()).first()

        # registered_via == "adopted" -- stage as a pending audit_log request.
        request_id = await install_dal.audit_log.async_insert(
            user_id=requested_by,
            action="managed_role.adoption_requested",
            target_type="managed_role",
            target_id=f"{pairing.platform}:{pairing.guild_id}:{role_id}",
            details={
                "tenant_id": tenant_id,
                "pairing_id": pairing_id,
                "community_id": community_id,
                "platform": pairing.platform,
                "guild_id": pairing.guild_id,
                "role_id": role_id,
            },
            created_at=now,
        )
        return PendingRoleRegistration(
            requestId=int(request_id),
            platform=pairing.platform,
            guildId=pairing.guild_id,
            roleId=role_id,
            communityId=community_id,
            pairingId=pairing_id,
            requestedAt=now.isoformat(),
        )


__all__ = [
    "SUPPORTED_PLATFORMS",
    "PendingRoleRegistration",
    "create_binding",
    "list_managed_roles",
    "list_pairings",
    "request_role_registration",
    "unbind",
]
