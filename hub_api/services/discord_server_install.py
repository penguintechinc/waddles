"""Add-a-Discord-server flow: bot-install OAuth that records the install AND pairs the guild.

A community admin clicks "Add Discord server" in the webui; hub-api mints a single-use,
browser-bound `state` (Redis, `GETDEL`), the browser goes through Discord's bot-authorize
consent screen, and Discord redirects back to the shared install callback
(`blueprints.v1.tenant_discord_install`) with `code` + `state` + `guild_id`. This module then:

1. verifies + consumes the state (forged / expired / replayed / foreign-browser all rejected),
2. exchanges `code` with the tenant's resolved Discord app credentials -- the token response's
   own `guild.id` is the authoritative guild (the `guild_id` query param is browser-controlled
   and only cross-checked), so an admin cannot pair a guild the bot was not just installed into,
3. records the bot install in the EXISTING connection model -- `platform_connections` (layer 2,
   `resource_type='discord_guild'`, `installed_by_user_id`, `created_at`, tenant) plus an
   approved `community_connection_access` grant (layer 3, the community) -- no new table,
4. creates the `guild_tenant_pairings` row (`direction='discord_to_twitch'` -- the only accepted
   value that keeps the Twitch reconciler off) with the admin's chosen `sync_enabled`.

State tokens carry the `srv.` prefix so the shared callback can dispatch without probing the
tenant-app install store. Nothing secret is logged; logs carry community/tenant/guild ids only.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from services import guild_pairing as pairing_svc
from services.bundle_telemetry import bundle_span, get_meter
from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    TransportUnavailable,
    grant_community_connection_access,
    upsert_platform_connection,
)
from services.discord_bot_install import (
    DISCORD_BOT_INSTALL_INTEGRATION_TYPE,
    DISCORD_BOT_INSTALL_PERMISSIONS,
    DISCORD_BOT_INSTALL_SCOPES,
)
from services.errors import ApiError
from services.install_state_binding import (
    ERR_STATE,
    InstallStateError,
    consume_bound,
    nonce_hash,
)
from services.role_sync_service import _resolve_tenant_for_community
from services.tenant_discord_install_state import DEFAULT_TTL_SECONDS
from services.tenant_discord_install_state import _redis_client as _install_redis_client
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

#: Every state this flow mints starts with this -- the shared callback's dispatch key.
STATE_PREFIX = "srv."
_STATE_KEY_PREFIX = "oauth:discord_server_install:state:"
_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_TOKEN_URL = "https://discord.com/api/oauth2/token"  # noqa: S105 - URL, not a credential
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: Pairing defaults for a server added through the webui (hub-api owns them, not the webui).
PAIRING_DIRECTION = "discord_to_twitch"
DEFAULT_ROLE_NAME_PREFIX = "Waddles"
PLATFORM_DISCORD = "discord"
RESOURCE_TYPE_GUILD = "discord_guild"

_meter = get_meter()
_install_counter = _meter.create_counter(
    "waddles_discord_server_install_total",
    description="Discord add-server installs by outcome (started/completed/rejected)",
)


@dataclass(slots=True, frozen=True)
class PendingServerInstall:
    """Decoded single-use context of one add-server `state` (no secrets in it)."""

    tenant_id: int
    community_id: int
    admin_user_id: int
    sync_enabled: bool
    redirect_uri: str


@dataclass(slots=True, frozen=True)
class ServerInstallResult:
    """Outcome of a completed add-server callback -- identifiers only."""

    community_id: int
    guild_id: str
    pairing_id: int
    connection_id: int
    created: bool


class ServerInstallError(ApiError):
    """Add-server failure that carries the community (when known) for the webui redirect."""

    def __init__(
        self, message: str, status_code: int, code: str, community_id: int | None = None
    ) -> None:
        """Record the typed error plus the community the browser should return to."""
        super().__init__(message, status_code, code)
        self.community_id = community_id


async def _resolve_app(
    dal: Any, community_id: int, resolver: CredentialResolver | None
) -> tuple[int, dict[str, Any]]:
    """`(tenant_id, discord app payload)` for the community's tenant, or a 503 ApiError."""
    tenant = _resolve_tenant_for_community(dal, community_id)
    if tenant is None:
        raise ApiError("Community tenant could not be resolved", 404, "NOT_FOUND")
    tenant_id, _slug, is_global = tenant
    try:
        creds = await (resolver or DefaultCredentialResolver()).resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform=PLATFORM_DISCORD
        )
    except TransportUnavailable as exc:
        raise ApiError(
            "Discord bot credentials are not configured for this tenant",
            503,
            "DISCORD_NOT_CONFIGURED",
        ) from exc
    payload = creds.payload
    if not payload.get("client_id") or not payload.get("client_secret"):
        raise ApiError(
            "Discord bot credentials are not configured for this tenant",
            503,
            "DISCORD_NOT_CONFIGURED",
        )
    return tenant_id, payload


async def build_server_install_url(
    dal: Any,
    *,
    community_id: int,
    admin_user_id: int,
    sync_enabled: bool,
    redirect_uri: str,
    binding_nonce: str,
    resolver: CredentialResolver | None = None,
) -> str:
    """Mint a bound single-use state and return Discord's bot-authorize URL for this community.

    The caller has already enforced tenant ownership + scope + feature flag, and MUST set
    `binding_nonce` as the browser-binding cookie.
    """
    tenant_id, app = await _resolve_app(dal, community_id, resolver)
    token = STATE_PREFIX + secrets.token_urlsafe(32)
    record = {
        "tenant_id": tenant_id,
        "community_id": community_id,
        "admin_user_id": admin_user_id,
        "sync_enabled": bool(sync_enabled),
        "redirect_uri": redirect_uri,
        "binding": nonce_hash(binding_nonce),
    }
    await _install_redis_client().set(
        f"{_STATE_KEY_PREFIX}{token}", json.dumps(record), ex=DEFAULT_TTL_SECONDS
    )
    _install_counter.add(1, {"outcome": "started"})
    logger.info(
        "discord_server_install.started community_id=%s tenant_id=%s", community_id, tenant_id
    )
    params = {
        "client_id": str(app["client_id"]),
        "scope": DISCORD_BOT_INSTALL_SCOPES,
        "permissions": str(DISCORD_BOT_INSTALL_PERMISSIONS),
        "integration_type": str(DISCORD_BOT_INSTALL_INTEGRATION_TYPE),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": token,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def consume_server_state(state: str, binding_nonce: str | None) -> PendingServerInstall:
    """Verify the browser binding, then atomically fetch-and-delete the add-server state.

    Raises `InstallStateError` (`invalid_or_expired_state` / `state_binding_mismatch`).
    """
    if not state.startswith(STATE_PREFIX) or len(state) > 128:
        raise InstallStateError(ERR_STATE)
    try:
        raw = await consume_bound(
            _install_redis_client(), f"{_STATE_KEY_PREFIX}{state}", binding_nonce
        )
    except InstallStateError:
        raise
    except Exception as exc:  # noqa: BLE001 - a Redis failure fails the flow closed
        logger.exception("discord_server_install.state_consume_failed")
        raise InstallStateError(ERR_STATE) from exc
    try:
        data = json.loads(raw)
        return PendingServerInstall(
            tenant_id=int(data["tenant_id"]),
            community_id=int(data["community_id"]),
            admin_user_id=int(data["admin_user_id"]),
            sync_enabled=bool(data["sync_enabled"]),
            redirect_uri=str(data["redirect_uri"]),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("discord_server_install.malformed_state")
        raise InstallStateError(ERR_STATE) from None


async def _exchange_code(
    app: dict[str, Any],
    *,
    code: str,
    redirect_uri: str,
    community_id: int,
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """Exchange `code` with the tenant's Discord app; returns the token payload (never logged)."""
    try:
        await validate_outbound_url(_TOKEN_URL, allowed_schemes=("https",))
    except ApiError as exc:
        raise ServerInstallError(
            "outbound URL blocked", 502, "install_failed", community_id
        ) from exc
    client = http_client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.post(
            _TOKEN_URL,
            data={
                "client_id": str(app["client_id"]),
                "client_secret": str(app["client_secret"]),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "discord_server_install.exchange_transport_error community_id=%s exc=%s",
            community_id,
            type(exc).__name__,
        )
        raise ServerInstallError(
            "Discord token exchange failed", 502, "install_failed", community_id
        ) from exc
    finally:
        if http_client is None:
            await client.aclose()
    if response.status_code // 100 != 2:
        logger.warning(
            "discord_server_install.exchange_non_2xx community_id=%s status=%s",
            community_id,
            response.status_code,
        )
        raise ServerInstallError(
            f"Discord token exchange failed: HTTP {response.status_code}",
            502,
            "install_failed",
            community_id,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise ServerInstallError(
            "Discord token exchange returned malformed JSON", 502, "install_failed", community_id
        )
    return payload


async def complete_server_install(
    dal: Any,
    *,
    code: str,
    state: str,
    guild_id_param: str | None,
    binding_nonce: str | None,
    http_client: httpx.AsyncClient | None = None,
    resolver: CredentialResolver | None = None,
) -> ServerInstallResult:
    """Verify state, exchange code, record the install, and pair the guild with the community.

    Idempotent on the pairing: re-adding an already-paired server refreshes the install record
    and returns the existing pairing (`created=False`) instead of failing the demo flow.
    """
    async with bundle_span("discord_server_install.complete"):
        try:
            pending = await consume_server_state(state, binding_nonce)
        except InstallStateError as exc:
            _install_counter.add(1, {"outcome": "rejected", "reason": exc.code})
            raise ServerInstallError(
                "invalid, expired, already-used, or foreign-browser install state", 400, exc.code
            ) from exc
        cid = pending.community_id
        if not code:
            _install_counter.add(1, {"outcome": "rejected", "reason": "missing_code"})
            raise ServerInstallError("missing authorization code", 400, "install_failed", cid)

        tenant_id, app = await _resolve_app(dal, cid, resolver)
        if tenant_id != pending.tenant_id:
            # The community moved tenants mid-flow -- never attach a guild across tenants.
            _install_counter.add(1, {"outcome": "rejected", "reason": "tenant_mismatch"})
            raise ServerInstallError("community tenant changed", 409, "install_failed", cid)

        token_payload = await _exchange_code(
            app,
            code=code,
            redirect_uri=pending.redirect_uri,
            community_id=cid,
            http_client=http_client,
        )
        guild = token_payload.get("guild")
        guild_id = str(guild.get("id", "")) if isinstance(guild, dict) else ""
        if not guild_id.isdigit():
            _install_counter.add(1, {"outcome": "rejected", "reason": "no_guild"})
            raise ServerInstallError(
                "Discord did not report the server the bot was added to",
                502,
                "install_failed",
                cid,
            )
        if guild_id_param and guild_id_param != guild_id:
            _install_counter.add(1, {"outcome": "rejected", "reason": "guild_mismatch"})
            raise ServerInstallError(
                "guild_id does not match the authorized server", 400, "install_failed", cid
            )
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise ServerInstallError(
                "Discord token exchange returned no token", 502, "install_failed", cid
            )

        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform=PLATFORM_DISCORD,
            resource_type=RESOURCE_TYPE_GUILD,
            resource_id=guild_id,
            access_token=access_token,
            refresh_token=token_payload.get("refresh_token") or None,
            installed_by_user_id=pending.admin_user_id,
        )
        grant_community_connection_access(
            dal,
            community_id=cid,
            connection_id=connection_id,
            status="approved",
            requested_by_user_id=pending.admin_user_id,
            approved_by_user_id=pending.admin_user_id,
        )

        existing = [
            p for p in pairing_svc.list_pairings(dal, cid) if p.discord_guild_id == guild_id
        ]
        if existing:
            pairing_id, created = existing[0].id, False
        else:
            pairing = pairing_svc.create_pairing(
                dal,
                cid,
                discord_guild_id=guild_id,
                direction=PAIRING_DIRECTION,
                role_name_prefix=DEFAULT_ROLE_NAME_PREFIX,
                sync_enabled=pending.sync_enabled,
                actor_user_id=pending.admin_user_id,
            )
            pairing_id, created = pairing.id, True

        _install_counter.add(1, {"outcome": "completed", "created": created})
        logger.info(
            "discord_server_install.completed community_id=%s tenant_id=%s guild_id=%s "
            "pairing_id=%s created=%s",
            cid,
            tenant_id,
            guild_id,
            pairing_id,
            created,
        )
        return ServerInstallResult(
            community_id=cid,
            guild_id=guild_id,
            pairing_id=pairing_id,
            connection_id=connection_id,
            created=created,
        )
