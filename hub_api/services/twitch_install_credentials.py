"""Persistence glue between `services.twitch_install_oauth` and `services.credential_resolver`.

`store_initial_credentials()` is the write side of the install flow
(`blueprints/v1/tenant_twitch_install.py`'s callback route): wraps
`services.credential_resolver.store_tenant_credentials` with migration
0034's documented `tenant_platform_credentials` payload shape
(`client_id`/`client_secret`/`bot_token`/`extra`), `bot_token` holding the
Twitch access token and `extra` holding the refresh token + scopes the
role-sync worker (Unit F) needs later.

`refresh_stored_credentials()` is the renew path Unit F's worker calls:
resolves the tenant's current Twitch credentials, exchanges the stored
refresh token for a new access/refresh pair, and persists the rotated
pair. Twitch invalidates a refresh token the instant it's used -- a
refresh call that fails here is therefore never retried with the same
token; it's treated as the stored refresh token having already been
consumed by someone else (replay/compromise) and the ENTIRE stored
credential row is deleted, forcing the tenant admin to re-run the install
flow from scratch -- security.md Service-to-Service Auth's "a reused
refresh token is treated as compromise and revokes the whole chain" rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    PlatformCredentials,
    TransportUnavailable,
    store_tenant_credentials,
)
from services.schema import bind_bar_citizen_tables
from services.twitch_install_oauth import TwitchOAuthError, TwitchTokenResult, refresh_access_token

logger = logging.getLogger(__name__)

PLATFORM_TWITCH = "twitch"

#: Default resolver instance -- stateless, safe to share (mirrors
#: `credential_resolver.py`'s own module-level convention of callers
#: constructing `DefaultCredentialResolver()` directly where no DI seam
#: is needed).
_resolver: CredentialResolver = DefaultCredentialResolver()


class TwitchRefreshRevokedError(Exception):
    """Raised when a stored refresh token was rejected by Twitch and the tenant's row was revoked.

    The tenant's `tenant_platform_credentials` row for `platform="twitch"`
    has already been deleted by the time this is raised -- callers (the
    role-sync worker) must stop syncing for this tenant and surface a
    "Twitch needs to be reconnected" state, never retry.
    """


def _payload_from_token(
    *, client_id: str, client_secret: str, token: TwitchTokenResult
) -> dict[str, Any]:
    """Build migration 0034's documented payload shape for a Twitch credentials row."""
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "bot_token": token.access_token,
        "extra": {
            "refresh_token": token.refresh_token,
            "scopes": token.scopes,
            "token_type": token.token_type,
            "obtained_at": datetime.now(UTC).isoformat(),
        },
    }


def store_initial_credentials(
    dal: Any,
    *,
    tenant_id: int,
    client_id: str,
    client_secret: str,
    token: TwitchTokenResult,
    installed_by_user_id: int,
) -> None:
    """Encrypt + upsert a tenant's freshly-authorized Twitch app credentials.

    Raises `services.errors.ApiError` (via `store_tenant_credentials`) if
    `tenant_id` is somehow the global tenant -- never reachable through
    `blueprints/v1/tenant_twitch_install.py`, which rejects tenant 0 before
    this is called, but kept as the DB-level backstop
    `store_tenant_credentials` already provides.
    """
    store_tenant_credentials(
        dal,
        tenant_id=tenant_id,
        is_global_tenant=False,
        platform=PLATFORM_TWITCH,
        payload=_payload_from_token(client_id=client_id, client_secret=client_secret, token=token),
        installed_by_user_id=installed_by_user_id,
    )


def _existing_installed_by_user_id(dal: Any, tenant_id: int) -> int | None:
    """Look up the `installed_by_user_id` column of the current stored row, if any.

    Preserves attribution across a refresh -- `refresh_stored_credentials()`
    rewrites the row's ciphertext but must not silently blank out who
    originally installed it.
    """
    bind_bar_citizen_tables(dal)
    t = dal.tenant_platform_credentials
    row = (
        dal((t.tenant_id == tenant_id) & (t.platform == PLATFORM_TWITCH))
        .select(t.installed_by_user_id)
        .first()
    )
    return int(row.installed_by_user_id) if row is not None and row.installed_by_user_id else None


def _revoke_stored_credentials(dal: Any, tenant_id: int) -> None:
    """Delete the tenant's `tenant_platform_credentials` row for Twitch -- forces re-install."""
    bind_bar_citizen_tables(dal)
    t = dal.tenant_platform_credentials
    try:
        dal((t.tenant_id == tenant_id) & (t.platform == PLATFORM_TWITCH)).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise


@dataclass(slots=True, frozen=True)
class RefreshedTwitchCredentials:
    """Result of a successful `refresh_stored_credentials()` call."""

    tenant_id: int
    access_token: str
    scopes: list[str]


async def refresh_stored_credentials(dal: Any, *, tenant_id: int) -> RefreshedTwitchCredentials:
    """Refresh a tenant's stored Twitch access token, rotating the refresh token.

    Raises `services.credential_resolver.TransportUnavailable` if the
    tenant has no stored Twitch credentials at all (never installed, or a
    prior revoke), or `TwitchRefreshRevokedError` if Twitch rejected the
    stored refresh token -- in the latter case the row has already been
    deleted before this returns. Never logs a token/secret value.
    """
    resolved: PlatformCredentials = await _resolver.resolve(
        dal, tenant_id=tenant_id, is_global_tenant=False, platform=PLATFORM_TWITCH
    )

    client_id = str(resolved.payload.get("client_id", ""))
    client_secret = str(resolved.payload.get("client_secret", ""))
    extra = resolved.payload.get("extra")
    current_refresh_token = str(extra.get("refresh_token", "")) if isinstance(extra, dict) else ""
    if not client_id or not client_secret or not current_refresh_token:
        raise TransportUnavailable(f"tenant {tenant_id} has an incomplete stored Twitch credential")

    try:
        token = await refresh_access_token(
            client_id=client_id, client_secret=client_secret, refresh_token=current_refresh_token
        )
    except TwitchOAuthError as exc:
        logger.warning(
            "twitch_install_credentials.refresh_rejected tenant_id=%s -- revoking", tenant_id
        )
        _revoke_stored_credentials(dal, tenant_id)
        raise TwitchRefreshRevokedError(
            f"tenant {tenant_id}: Twitch rejected the stored refresh token; "
            "credentials revoked, re-install required"
        ) from exc

    store_initial_credentials(
        dal,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        token=token,
        installed_by_user_id=_existing_installed_by_user_id(dal, tenant_id) or 0,
    )
    return RefreshedTwitchCredentials(
        tenant_id=tenant_id, access_token=token.access_token, scopes=token.scopes
    )
