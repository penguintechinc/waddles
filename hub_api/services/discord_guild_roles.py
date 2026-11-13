"""List a paired Discord guild's roles for the role-sync mapping dropdown.

Resolves the community's guild from `guild_tenant_pairings`, the bot token
through the same `CredentialResolver` seam role-sync uses, and calls
`GET /guilds/{guild}/roles`. Every failure mode raises a distinct
`ApiError` code so the webui can show an actionable message. The bot token
is never logged; logs carry community_id/guild_id/op/counts/status only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from services import guild_pairing as pairing_svc
from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    TransportUnavailable,
)
from services.errors import ApiError, bad_request
from services.role_sync_service import DISCORD_API_BASE, _resolve_tenant_for_community

logger = logging.getLogger(__name__)

#: Discord JSON error code for "Missing Access" -- what a bot NOT in the guild gets (403).
_DISCORD_MISSING_ACCESS = 50001
_REQUEST_TIMEOUT_SECONDS = 10.0


#: Permission bits that make a role privileged: a mirror must never grant one of these.
#: ADMINISTRATOR, MANAGE_GUILD, MANAGE_ROLES, MANAGE_CHANNELS, MANAGE_WEBHOOKS, BAN_MEMBERS,
#: KICK_MEMBERS, MODERATE_MEMBERS, MENTION_EVERYONE.
PRIVILEGED_PERMISSION_MASK = (
    0x8 | 0x20 | 0x10000000 | 0x10 | 0x20000000 | 0x4 | 0x2 | 0x10000000000 | 0x20000
)


def is_privileged_role(permissions: int, managed: bool) -> bool:
    """True for a managed (bot/integration) role or one carrying any privileged permission."""
    return bool(managed) or bool(int(permissions) & PRIVILEGED_PERMISSION_MASK)


@dataclass(slots=True, frozen=True)
class DiscordRole:
    """One Discord role -- `id` is a string snowflake (never a JS-lossy int).

    `privileged` is computed server-side (see `is_privileged_role`) so the webui stays thin.
    """

    id: str
    name: str
    color: int
    position: int
    permissions: int = 0
    managed: bool = False
    privileged: bool = False


def _select_guild_id(dal: Any, community_id: int, guild_id: str | None) -> str:
    """Pick the guild to query from this community's pairings; never an arbitrary guild."""
    pairings = pairing_svc.list_pairings(dal, community_id)
    if not pairings:
        raise ApiError(
            "This community has no Discord guild pairing", 404, "DISCORD_PAIRING_NOT_FOUND"
        )
    paired = [p.discord_guild_id for p in pairings]
    if guild_id:
        if guild_id not in paired:
            raise ApiError(
                "That guild is not paired with this community", 404, "DISCORD_PAIRING_NOT_FOUND"
            )
        return guild_id
    if len(set(paired)) > 1:
        raise bad_request("Community has multiple paired guilds; pass ?guild_id=")
    return paired[0]


def _raise_for_status(response: httpx.Response, *, community_id: int, guild_id: str) -> None:
    """Map a non-2xx Discord response to a specific `ApiError`."""
    status = response.status_code
    if status < 400:
        return
    logger.warning(
        "discord_roles.upstream_error community_id=%s guild_id=%s status=%s",
        community_id,
        guild_id,
        status,
    )
    if status == 404:
        raise ApiError("The Discord bot is not in this guild", 409, "DISCORD_BOT_NOT_IN_GUILD")
    if status == 403:
        try:
            discord_code = response.json().get("code")
        except (ValueError, AttributeError):
            discord_code = None
        if discord_code == _DISCORD_MISSING_ACCESS:
            raise ApiError("The Discord bot is not in this guild", 409, "DISCORD_BOT_NOT_IN_GUILD")
        raise ApiError(
            "The Discord bot lacks permission to read this guild's roles",
            502,
            "DISCORD_MISSING_PERMISSIONS",
        )
    if status == 401:
        raise ApiError("The Discord bot token was rejected", 502, "DISCORD_BOT_TOKEN_INVALID")
    if status == 429:
        raise ApiError(
            "Discord rate limited this request; retry shortly", 503, "DISCORD_RATE_LIMITED"
        )
    raise ApiError(f"Discord API error (HTTP {status})", 502, "DISCORD_UNAVAILABLE")


async def list_guild_roles(
    dal: Any,
    community_id: int,
    *,
    guild_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    credential_resolver: CredentialResolver | None = None,
    api_base: str = DISCORD_API_BASE,
) -> list[DiscordRole]:
    """Return the paired guild's roles (excluding `@everyone`), highest position first."""
    target_guild = _select_guild_id(dal, community_id, guild_id)
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

    client = http_client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await client.get(
                f"{api_base}/guilds/{target_guild}/roles",
                headers={"Authorization": f"Bot {bot_token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "discord_roles.transport_error community_id=%s guild_id=%s exc=%s",
                community_id,
                target_guild,
                type(exc).__name__,
            )
            raise ApiError("Discord API is unreachable", 502, "DISCORD_UNAVAILABLE") from exc
    finally:
        if http_client is None:
            await client.aclose()

    _raise_for_status(response, community_id=community_id, guild_id=target_guild)
    try:
        raw = response.json()
        roles = [
            DiscordRole(
                id=str(r["id"]),
                name=str(r["name"]),
                color=int(r.get("color", 0)),
                position=int(r.get("position", 0)),
                permissions=int(r.get("permissions", 0) or 0),
                managed=bool(r.get("managed", False)),
                privileged=is_privileged_role(
                    int(r.get("permissions", 0) or 0), bool(r.get("managed", False))
                ),
            )
            for r in raw
            if str(r["id"]) != target_guild  # @everyone shares the guild's snowflake
        ]
    except (ValueError, TypeError, KeyError) as exc:
        logger.error(
            "discord_roles.malformed_response community_id=%s guild_id=%s",
            community_id,
            target_guild,
        )
        raise ApiError(
            "Discord returned a malformed roles payload", 502, "DISCORD_UNAVAILABLE"
        ) from exc
    roles.sort(key=lambda r: r.position, reverse=True)
    logger.info(
        "discord_roles.listed community_id=%s guild_id=%s count=%s",
        community_id,
        target_guild,
        len(roles),
    )
    return roles


async def verify_bot_in_guild(
    dal: Any,
    community_id: int,
    guild_id: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    credential_resolver: CredentialResolver | None = None,
    api_base: str = DISCORD_API_BASE,
) -> None:
    """Raise 409 `DISCORD_BOT_NOT_IN_GUILD` unless the tenant's bot is a member of `guild_id`.

    Used by the manual "pair server by ID" path so a pairing is only ever created for a guild
    the bot can actually act in. Same credential seam + error mapping as `list_guild_roles`.
    """
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
    client = http_client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            response = await client.get(
                f"{api_base}/guilds/{guild_id}", headers={"Authorization": f"Bot {bot_token}"}
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "discord_roles.verify_transport_error community_id=%s guild_id=%s exc=%s",
                community_id,
                guild_id,
                type(exc).__name__,
            )
            raise ApiError("Discord API is unreachable", 502, "DISCORD_UNAVAILABLE") from exc
    finally:
        if http_client is None:
            await client.aclose()
    _raise_for_status(response, community_id=community_id, guild_id=guild_id)
    logger.info("discord_roles.bot_in_guild community_id=%s guild_id=%s", community_id, guild_id)
