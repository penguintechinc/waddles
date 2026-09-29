"""Per-tenant Discord OAuth2 bot-install flow (#500/#501 follow-on, contract Sec2).

Discord allows one application install per guild, so consent is the
tenant's own OAuth2 bot-install callback (`bot` + `applications.commands`
scopes) against ITS OWN Discord application -- never a shared-bot
"Manage Server" permission check (owner correction, contract doc Sec2).

Flow:
1. `build_authorize_url()` -- tenant admin starts the flow; looks up the
   tenant's own `application_id` (`tenant_platform_credentials`, never the
   platform bot), mints a `services.guild_oauth_state` HMAC-signed state,
   returns Discord's `authorize` URL.
2. Discord redirects the admin's browser back with `code`, `guild_id`,
   `permissions`, and the `state` we minted (standard Discord bot-install
   redirect shape -- `guild_id`/`permissions` arrive as query params on
   the callback itself, not inside the token-exchange response body).
3. `complete_install()` -- verifies+consumes `state` (forged/expired/
   replayed all rejected by `guild_oauth_state.verify_and_consume_state`),
   exchanges `code` for a token using the TENANT'S OWN client credentials
   (confirms the code is genuine and was actually issued to this
   application), then upserts an `active` `guild_tenant_pairings` row for
   `(platform, guild_id, tenant_id)`.
4. `revoke_pairing()` -- flips a pairing to `revoked`, called either by a
   tenant admin action or by the internal revocation endpoint handling a
   Discord guild-delete/integration-removed gateway event (contract Sec5).

Exchange goes through `httpx`, matching `services/oauth_providers.py`'s
own HTTP-client choice and timeout budget -- kept as a separate, minimal
function here rather than added to that module's `PROVIDERS` registry,
since bot-install credentials are per-tenant (this module always resolves
them from `tenant_platform_credentials`), while every `PROVIDERS` entry
there resolves a single pair of env vars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from penguin_dal import AsyncDB

from services import bundle_audit
from services.bundle_install_dal import raw_sql_write
from services.errors import ApiError, bad_request, not_found
from services.guild_oauth_state import StateError, mint_state, verify_and_consume_state
from services.tenant_platform_credentials_service import (
    PLATFORM_DISCORD,
    decrypt_client_credentials,
)

logger = logging.getLogger(__name__)

_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_TOKEN_URL = "https://discord.com/api/oauth2/token"  # noqa: S105 - URL, not a credential
_BOT_INSTALL_SCOPES = "bot applications.commands"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class OAuthInstallError(ApiError):
    """Raised for any bot-install flow failure the caller should see as a clean 4xx."""


@dataclass(slots=True, frozen=True)
class InstallResult:
    """Outcome of a completed bot-install callback -- the activated pairing's identity."""

    pairing_id: str
    guild_id: str
    tenant_id: int
    granted_permissions: int | None


async def build_authorize_url(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    admin_user_id: int,
    redirect_uri: str,
    permissions: int,
) -> str:
    """Build the tenant's own Discord bot-install authorize URL.

    Raises 404 (via `not_found`) if the tenant has no configured Discord
    app -- fail closed, never falls back to a platform application id.
    """
    credentials = await decrypt_client_credentials(
        install_dal, tenant_id=tenant_id, platform=PLATFORM_DISCORD
    )
    if credentials is None:
        raise not_found("tenant Discord app not configured")
    application_id, _client_secret = credentials

    state = mint_state(tenant_id=tenant_id, admin_user_id=admin_user_id)
    params = {
        "client_id": application_id,
        "scope": _BOT_INSTALL_SCOPES,
        "permissions": str(permissions),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def _exchange_code(
    *, application_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """POST the authorization code to Discord's token endpoint using the tenant's own app.

    Mocked in every test (`services/oauth_providers.py`'s own docstring
    precedent: no real Discord calls in the test suite). Raises
    `OAuthInstallError` on any non-2xx or malformed response -- message
    carries only the HTTP status, never a token or raw body.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": application_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise OAuthInstallError("discord token exchange failed: network error", 502) from exc

    if response.status_code >= 400:
        raise OAuthInstallError(f"discord token exchange failed: HTTP {response.status_code}", 502)
    try:
        return dict(response.json())
    except ValueError as exc:
        raise OAuthInstallError("discord token exchange returned malformed JSON", 502) from exc


async def complete_install(
    install_dal: AsyncDB,
    *,
    state: str,
    code: str,
    guild_id: str,
    permissions: int | None,
    redirect_uri: str,
    platform: str = PLATFORM_DISCORD,
) -> InstallResult:
    """Verify `state`, exchange `code`, and activate `(platform, guild_id, tenant_id)`.

    Raises `OAuthInstallError` (400) for a forged/expired/replayed state
    or a missing/expired tenant app; the caller (blueprint route) converts
    any `ApiError` to the standard JSON error envelope -- never leaks
    which specific check failed beyond a generic message, avoiding a
    state-guessing oracle.
    """
    try:
        verified = await verify_and_consume_state(state)
    except StateError as exc:
        raise OAuthInstallError("invalid, expired, or already-used install state", 400) from exc

    tenant_id = verified.tenant_id
    if not guild_id:
        raise bad_request("guild_id is required")

    credentials = await decrypt_client_credentials(
        install_dal, tenant_id=tenant_id, platform=platform
    )
    if credentials is None:
        raise OAuthInstallError("tenant Discord app not configured", 400)
    application_id, client_secret = credentials

    await _exchange_code(
        application_id=application_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )

    now = datetime.now(UTC)
    pairing_id = await _upsert_active_pairing(
        install_dal,
        platform=platform,
        guild_id=guild_id,
        tenant_id=tenant_id,
        installed_by_user_id=verified.admin_user_id,
        granted_permissions=permissions,
        oauth_scopes=_BOT_INSTALL_SCOPES,
        now=now,
    )

    await bundle_audit.record(
        install_dal,
        actor_id=verified.admin_user_id,
        action="guild_tenant_pairings.install",
        target_type="guild_tenant_pairings",
        target_id=pairing_id,
        details={"platform": platform, "guild_id": guild_id, "tenant_id": tenant_id},
    )

    return InstallResult(
        pairing_id=pairing_id,
        guild_id=guild_id,
        tenant_id=tenant_id,
        granted_permissions=permissions,
    )


async def _upsert_active_pairing(
    install_dal: AsyncDB,
    *,
    platform: str,
    guild_id: str,
    tenant_id: int,
    installed_by_user_id: int,
    granted_permissions: int | None,
    oauth_scopes: str,
    now: datetime,
) -> str:
    """Insert-or-reactivate the `(platform, guild_id, tenant_id)` pairing row.

    Raw SQL (`ON CONFLICT`) rather than the `AsyncDB` query builder -- the
    table's PK is a server-generated UUID (`gen_random_uuid()`), and this
    needs an atomic upsert against migration 0038's own
    `UNIQUE (platform, guild_id, tenant_id)` constraint, which the
    `TableProxy` insert/update split used elsewhere in this port cannot
    express as a single statement.
    """
    result = await raw_sql_write(
        install_dal,
        """
        INSERT INTO guild_tenant_pairings (
            platform, guild_id, tenant_id, status, installed_by_user_id,
            granted_permissions, oauth_scopes, consent_at, last_verified_at,
            created_at, updated_at
        ) VALUES (
            :platform, :guild_id, :tenant_id, 'active', :installed_by_user_id,
            :granted_permissions, :oauth_scopes, :now, :now, :now, :now
        )
        ON CONFLICT (platform, guild_id, tenant_id) DO UPDATE SET
            status = 'active',
            installed_by_user_id = EXCLUDED.installed_by_user_id,
            granted_permissions = EXCLUDED.granted_permissions,
            oauth_scopes = EXCLUDED.oauth_scopes,
            consent_at = EXCLUDED.consent_at,
            last_verified_at = EXCLUDED.last_verified_at,
            revoked_at = NULL,
            revoked_by = NULL,
            revoked_by_user_id = NULL,
            updated_at = EXCLUDED.updated_at
        RETURNING id
        """,
        {
            "platform": platform,
            "guild_id": guild_id,
            "tenant_id": tenant_id,
            "installed_by_user_id": installed_by_user_id,
            "granted_permissions": granted_permissions,
            "oauth_scopes": oauth_scopes,
            "now": now,
        },
    )
    row = result.first()
    if row is None:  # pragma: no cover - RETURNING id always yields a row on success
        raise OAuthInstallError("pairing upsert did not return a row", 500)
    return str(row.id)


async def revoke_pairing(
    install_dal: AsyncDB,
    *,
    platform: str,
    guild_id: str,
    tenant_id: int,
    revoked_by: str,
    revoked_by_user_id: int | None,
) -> None:
    """Flip a pairing to `revoked` -- tenant-admin action or a guild-removal event (contract Sec5).

    `revoked_by` must be one of migration 0038's own CHECK values
    (`tenant_admin`, `guild_removed_bot`, `integration_removed`) -- an
    invalid value fails the DB constraint rather than being silently
    accepted.
    """
    now = datetime.now(UTC)
    result = await raw_sql_write(
        install_dal,
        """
        UPDATE guild_tenant_pairings
        SET status = 'revoked', revoked_at = :now, revoked_by = :revoked_by,
            revoked_by_user_id = :revoked_by_user_id, updated_at = :now
        WHERE platform = :platform AND guild_id = :guild_id AND tenant_id = :tenant_id
          AND status = 'active'
        RETURNING id
        """,
        {
            "now": now,
            "revoked_by": revoked_by,
            "revoked_by_user_id": revoked_by_user_id,
            "platform": platform,
            "guild_id": guild_id,
            "tenant_id": tenant_id,
        },
    )
    row = result.first()
    if row is None:
        raise not_found("no active pairing found for that guild/tenant")

    await bundle_audit.record(
        install_dal,
        actor_id=revoked_by_user_id,
        action="guild_tenant_pairings.revoke",
        target_type="guild_tenant_pairings",
        target_id=str(row.id),
        details={
            "platform": platform,
            "guild_id": guild_id,
            "tenant_id": tenant_id,
            "revoked_by": revoked_by,
        },
    )
