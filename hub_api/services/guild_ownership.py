"""Proof that the admin pairing a Discord guild actually controls it (role-sync S3 fix).

There is no per-guild bot-install record in this codebase: `discord_install_service` stores only
the tenant's Discord application credentials, never which guilds installed it. The closest proof
available, checked live with the TENANT'S OWN bot token:

1. The tenant's bot is a member of the guild (`GET /guilds/{id}` succeeds). For a tenant with its
   own Discord app this alone proves the guild installed that tenant's bot.
2. The acting Waddles user's linked Discord account is the guild owner, or holds a role granting
   ADMINISTRATOR or MANAGE_GUILD. This closes the shared-SaaS-bot gap: being in a guild that also
   has the Waddles bot is not enough -- you must administer it.

Fails closed: any upstream error, missing link or missing permission refuses the pairing. Logs
carry community/guild ids and outcome codes only (never a token or Discord user id).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    TransportUnavailable,
)
from services.errors import ApiError
from services.role_sync_service import DISCORD_API_BASE, _resolve_tenant_for_community
from services.schema import bind_auth_tables

logger = logging.getLogger(__name__)

_ADMINISTRATOR = 0x8
_MANAGE_GUILD = 0x20
_TIMEOUT_SECONDS = 10.0


def _refuse(message: str, code: str, *, community_id: int, guild_id: str) -> ApiError:
    """Build the refusal and log its code (ids only)."""
    logger.warning(
        "guild_pairing.ownership_refused community_id=%s guild_id=%s code=%s",
        community_id,
        guild_id,
        code,
    )
    return ApiError(message, 403, code)


def _linked_discord_id(dal: Any, hub_user_id: int | None) -> str | None:
    """The actor's linked Discord user id, or None."""
    if hub_user_id is None:
        return None
    bind_auth_tables(dal)
    t = dal.hub_user_identities
    row = dal((t.hub_user_id == hub_user_id) & (t.platform == "discord")).select().first()
    return str(row.platform_user_id) if row is not None else None


async def verify_guild_control(
    dal: Any,
    community_id: int,
    guild_id: str,
    *,
    actor_user_id: int | None,
    http_client: httpx.AsyncClient | None = None,
    credential_resolver: CredentialResolver | None = None,
    api_base: str = DISCORD_API_BASE,
) -> None:
    """Raise `ApiError` unless the tenant bot is in `guild_id` and the actor administers it."""
    tenant = _resolve_tenant_for_community(dal, community_id)
    if tenant is None:
        raise ApiError("Community tenant could not be resolved", 404, "NOT_FOUND")
    tenant_id, _slug, is_global = tenant
    resolver = credential_resolver or DefaultCredentialResolver()
    try:
        creds = await resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="discord"
        )
    except TransportUnavailable as exc:
        raise ApiError(
            "Discord bot credentials are not configured for this tenant",
            503,
            "DISCORD_NOT_CONFIGURED",
        ) from exc
    bot_token = str(creds.payload.get("bot_token", ""))
    if not bot_token:
        raise ApiError(
            "Discord bot credentials are not configured for this tenant",
            503,
            "DISCORD_NOT_CONFIGURED",
        )

    actor_discord_id = _linked_discord_id(dal, actor_user_id)
    if actor_discord_id is None:
        raise _refuse(
            "Link your Discord account to prove you administer this server",
            "guild_control_unlinked",
            community_id=community_id,
            guild_id=guild_id,
        )

    headers = {"Authorization": f"Bot {bot_token}"}
    client = http_client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    try:
        guild_resp = await client.get(f"{api_base}/guilds/{guild_id}", headers=headers)
        if guild_resp.status_code != 200:
            raise _refuse(
                "This tenant's Discord bot is not installed in that server",
                "guild_bot_not_installed",
                community_id=community_id,
                guild_id=guild_id,
            )
        guild = guild_resp.json()
        if str(guild.get("owner_id", "")) == actor_discord_id:
            return
        member_resp = await client.get(
            f"{api_base}/guilds/{guild_id}/members/{actor_discord_id}", headers=headers
        )
        if member_resp.status_code != 200:
            raise _refuse(
                "You are not a member of that Discord server",
                "guild_control_not_member",
                community_id=community_id,
                guild_id=guild_id,
            )
        member_roles = {str(r) for r in member_resp.json().get("roles", [])}
        roles = guild.get("roles") or []
        perms = 0
        for role in roles:
            rid = str(role.get("id"))
            if rid in member_roles or rid == guild_id:  # @everyone shares the guild id
                perms |= int(role.get("permissions", 0) or 0)
    except httpx.HTTPError as exc:
        raise ApiError("Discord API is unreachable", 502, "DISCORD_UNAVAILABLE") from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError("Discord returned a malformed payload", 502, "DISCORD_UNAVAILABLE") from exc
    finally:
        if http_client is None:
            await client.aclose()
    if not perms & (_ADMINISTRATOR | _MANAGE_GUILD):
        raise _refuse(
            "Only a Discord server owner or administrator can pair it",
            "guild_control_insufficient",
            community_id=community_id,
            guild_id=guild_id,
        )
