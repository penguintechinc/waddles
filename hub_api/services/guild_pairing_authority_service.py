"""Guild-authority (cross-tenant) operations over the pairing schema (#500/#501).

Distinct actor and authorization model from `services/guild_pairing_service.py`
(tenant admin): a **guild authority** is whoever currently holds Discord
"Manage Server"-equivalent authority over one specific `(platform, guild_id)`
-- not a hub-api tenant role, and not necessarily a member of any tenant this
guild is paired with. Their actions are inherently cross-tenant: approving an
adopted-role request submitted by one tenant, revoking a *different* tenant's
pairing, or listing every tenant/community paired with their own guild.

**Identity check is injected, not implemented here (deliberate boundary with
`feature/guild-pairing-oauth`).** Verifying "does hub user X actually hold
guild-authority over guild Y on Discord right now" requires a live Discord
API call through that tenant's -- or the platform's -- bot credentials
(`tenant_platform_credentials`, resolved via the OAuth agent's credential-
resolution module per the contract doc's Sec2). This module never resolves
credentials or calls Discord directly: it depends on the `GuildAuthorityVerifier`
Protocol below, injected by the app factory once the OAuth agent's module
provides a real implementation (`app.config["guild_authority_verifier"]`).
Fails closed if unconfigured (`ApiError` 503) -- never silently authorizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from penguin_dal import AsyncDB

from services import bundle_audit
from services.bundle_telemetry import bundle_span
from services.errors import ApiError, not_found, unprocessable


@runtime_checkable
class GuildAuthorityVerifier(Protocol):
    """Verifies whether a hub user currently holds Discord guild-authority over a guild.

    Implemented outside this module (the OAuth agent's `feature/guild-pairing-oauth`
    work) using that guild's paired tenant bot credential(s) to check the
    caller's live Discord guild membership/permissions (e.g. `MANAGE_GUILD`)
    or guild-owner identity. Injected via dependency injection (app config),
    never imported directly, so this module has zero Discord OAuth surface.
    """

    async def verify(self, *, platform: str, guild_id: str, hub_user_id: int) -> bool:
        """Return `True` iff `hub_user_id` currently holds guild-authority over the guild."""
        ...


def _require_verifier(verifier: GuildAuthorityVerifier | None) -> GuildAuthorityVerifier:
    """Fail closed (503) if no verifier has been wired in yet -- never authorize by default."""
    if verifier is None:
        raise ApiError(
            "Guild-authority verification is not configured on this deployment",
            503,
            "GUILD_AUTHORITY_VERIFIER_UNAVAILABLE",
        )
    return verifier


async def _assert_authority(
    verifier: GuildAuthorityVerifier | None,
    *,
    platform: str,
    guild_id: str,
    hub_user_id: int,
) -> None:
    live_verifier = _require_verifier(verifier)
    if not await live_verifier.verify(
        platform=platform, guild_id=guild_id, hub_user_id=hub_user_id
    ):
        raise ApiError(
            "You do not hold guild-authority over this Discord guild",
            403,
            "NOT_GUILD_AUTHORITY",
        )


@dataclass(slots=True, frozen=True)
class GuildOverviewEntry:
    """One paired-tenant summary row for a guild -- NO member data (task requirement)."""

    pairingId: str
    tenantId: int
    status: str
    boundCommunityIds: list[int]
    ownedRoleIds: list[str]


async def approve_adopted_role(
    install_dal: AsyncDB,
    verifier: GuildAuthorityVerifier | None,
    *,
    request_id: int,
    approver_hub_user_id: int,
) -> Any:
    """Approve a pending `adopted` role registration (audit-log-staged by `guild_pairing_service`).

    Resolves the staged `audit_log` row (`action='managed_role.adoption_requested'`),
    verifies the approver's guild-authority over that request's guild, then
    inserts the real `managed_roles` row with `approved_by_user_id` set --
    satisfying `chk_managed_roles_adopted_approval` for the first time.
    """
    async with bundle_span("hub.guild_pairing.approve_adopted_role", request_id=request_id):
        rows = await install_dal(
            (install_dal.audit_log.id == request_id)
            & (install_dal.audit_log.action == "managed_role.adoption_requested")
        ).select()
        request_row = rows.first()
        if request_row is None:
            raise not_found("Role adoption request not found")

        details = dict(request_row.details or {})
        platform = details.get("platform")
        guild_id = details.get("guild_id")
        role_id = details.get("role_id")
        pairing_id = details.get("pairing_id")
        tenant_id = details.get("tenant_id")
        community_id = details.get("community_id")
        if not all([platform, guild_id, role_id, pairing_id, tenant_id, community_id]):
            raise unprocessable("Role adoption request is malformed")
        assert isinstance(platform, str)  # nosec B101 -- validated by the all([...]) check above
        assert isinstance(guild_id, str)  # nosec B101

        await _assert_authority(
            verifier, platform=platform, guild_id=guild_id, hub_user_id=approver_hub_user_id
        )

        existing = await install_dal(
            (install_dal.managed_roles.platform == platform)
            & (install_dal.managed_roles.guild_id == guild_id)
            & (install_dal.managed_roles.role_id == role_id)
            & (install_dal.managed_roles.status == "active")
        ).select()
        if existing.first() is not None:
            raise ApiError(
                "This role is already registered to a community in this guild.",
                409,
                "ROLE_ALREADY_OWNED",
            )

        now = datetime.now(UTC)
        new_id = await install_dal.managed_roles.async_insert(
            platform=platform,
            guild_id=guild_id,
            role_id=role_id,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            owning_community_id=community_id,
            registered_via="adopted",
            approved_by_user_id=approver_hub_user_id,
            approved_at=now,
            status="active",
            created_at=now,
            updated_at=now,
        )
        await bundle_audit.record(
            install_dal,
            actor_id=approver_hub_user_id,
            action="guild_pairing.role_adoption_approved",
            target_type="managed_role",
            target_id=str(new_id),
            details={"request_id": request_id, "tenant_id": tenant_id, "role_id": role_id},
        )
        return (await install_dal(install_dal.managed_roles.id == new_id).select()).first()


async def revoke_pairing(
    install_dal: AsyncDB,
    verifier: GuildAuthorityVerifier | None,
    *,
    pairing_id: str,
    revoker_hub_user_id: int,
) -> Any:
    """Guild-side revocation cascade: deactivate bindings, mark roles pending_cleanup.

    Flips `guild_tenant_pairings.status` to `revoked` (`revoked_by='guild_removed_bot'`
    proxy -- a guild authority revoking is modeled the same as the bot being
    removed, per the contract doc's revocation section), deactivates every
    `community_channel_bindings` row under it, and marks every `managed_roles`
    row `pending_cleanup` for the data plane's best-effort unwind pass. Both
    views (`v_guild_routing`/`v_managed_roles_active`) stop returning any row
    under this pairing immediately (they filter on pairing status).
    """
    async with bundle_span("hub.guild_pairing.revoke_pairing", pairing_id=pairing_id):
        rows = await install_dal(install_dal.guild_tenant_pairings.id == pairing_id).select()
        pairing = rows.first()
        if pairing is None:
            raise not_found("Guild pairing not found")

        await _assert_authority(
            verifier,
            platform=pairing.platform,
            guild_id=pairing.guild_id,
            hub_user_id=revoker_hub_user_id,
        )

        now = datetime.now(UTC)
        await install_dal(install_dal.guild_tenant_pairings.id == pairing_id).update(
            status="revoked",
            revoked_at=now,
            revoked_by="guild_removed_bot",
            revoked_by_user_id=revoker_hub_user_id,
            updated_at=now,
        )
        await install_dal(install_dal.community_channel_bindings.pairing_id == pairing_id).update(
            status="revoked", updated_at=now
        )
        await install_dal(install_dal.managed_roles.pairing_id == pairing_id).update(
            status="pending_cleanup", updated_at=now
        )
        await bundle_audit.record(
            install_dal,
            actor_id=revoker_hub_user_id,
            action="guild_pairing.revoked_by_guild_authority",
            target_type="guild_tenant_pairing",
            target_id=str(pairing_id),
            details={"tenant_id": pairing.tenant_id, "guild_id": pairing.guild_id},
        )
        return (
            await install_dal(install_dal.guild_tenant_pairings.id == pairing_id).select()
        ).first()


async def guild_overview(
    install_dal: AsyncDB,
    verifier: GuildAuthorityVerifier | None,
    *,
    platform: str,
    guild_id: str,
    requester_hub_user_id: int,
) -> list[GuildOverviewEntry]:
    """Which tenants/communities are paired with this guild, and which roles each owns.

    NO member data (task requirement) -- only pairing/binding/role ids.
    """
    async with bundle_span("hub.guild_pairing.guild_overview", guild_id=guild_id):
        await _assert_authority(
            verifier, platform=platform, guild_id=guild_id, hub_user_id=requester_hub_user_id
        )

        pairings = await install_dal(
            (install_dal.guild_tenant_pairings.platform == platform)
            & (install_dal.guild_tenant_pairings.guild_id == guild_id)
        ).select()

        entries: list[GuildOverviewEntry] = []
        for pairing in pairings:
            bindings = await install_dal(
                (install_dal.community_channel_bindings.pairing_id == pairing.id)
                & (install_dal.community_channel_bindings.status == "active")
            ).select()
            roles = await install_dal(
                (install_dal.managed_roles.pairing_id == pairing.id)
                & (install_dal.managed_roles.status == "active")
            ).select()
            entries.append(
                GuildOverviewEntry(
                    pairingId=str(pairing.id),
                    tenantId=pairing.tenant_id,
                    status=pairing.status,
                    boundCommunityIds=sorted({b.community_id for b in bindings}),
                    ownedRoleIds=sorted({r.role_id for r in roles}),
                )
            )
        return entries


__all__ = [
    "GuildAuthorityVerifier",
    "GuildOverviewEntry",
    "approve_adopted_role",
    "guild_overview",
    "revoke_pairing",
]
