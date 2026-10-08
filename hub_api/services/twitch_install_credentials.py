"""Persistence glue between `services.twitch_install_oauth` and `services.credential_resolver`.

**Ported off migration 0034's conflated shape onto 0035's three-layer
model** (`docs/CONNECTION_MODEL.local.md`) -- the tenant's own Twitch app
credentials (`client_id`/`client_secret`) live in layer 1
(`tenant_platform_apps`, via `store_tenant_credentials()`); the per-channel
OAuth grant (`access_token`/`refresh_token`) lives in layer 2
(`platform_connections`, via `upsert_platform_connection()`/`get_platform_
connection_for_tenant()`), keyed by `resource_type="twitch_channel"` and
`resource_id=<the authorizing broadcaster's own Twitch user id>` (resolved
once via `twitch_install_oauth.fetch_token_user_id()` at install time, then
reused unchanged across refreshes -- a channel's Twitch user id never
changes). No layer-3 `community_connection_access` grant is created here:
this flow is tenant-scoped (a tenant admin authorizing their own channel),
with no community in play yet -- wiring a community's opt-in to reuse this
connection is Unit F/G's job (role-sync worker / admin reuse+approval UI),
tracked in the design doc, not this module.

`store_initial_credentials()` is the write side of the install flow
(`blueprints/v1/tenant_twitch_install.py`'s callback route): persists the
app credentials (layer 1) then the channel connection (layer 2).

`refresh_stored_credentials()` is the renew path Unit F's worker calls:
resolves the tenant's app credentials (layer 1) and current channel
connection (layer 2), exchanges the stored refresh token for a new
access/refresh pair, and persists the rotated pair back to layer 2 only --
layer 1's app credentials are never touched by a channel-token refresh.
Twitch invalidates a refresh token the instant it's used -- a refresh call
that fails here is therefore never retried with the same token; it's
treated as the stored refresh token having already been consumed by
someone else (replay/compromise) and the layer-2 CONNECTION row (not the
layer-1 app credentials, which remain valid) is deleted, forcing the
tenant admin to re-authorize that channel -- security.md Service-to-Service
Auth's "a reused refresh token is treated as compromise and revokes the
whole chain" rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    PlatformCredentials,
    TransportUnavailable,
    delete_platform_connection,
    get_platform_connection_for_tenant,
    store_tenant_credentials,
    upsert_platform_connection,
)
from services.twitch_install_oauth import (
    TwitchOAuthError,
    TwitchTokenResult,
    fetch_token_user_id,
    refresh_access_token,
)

logger = logging.getLogger(__name__)

PLATFORM_TWITCH = "twitch"

#: `platform_connections.resource_type` this module always writes/reads under.
RESOURCE_TYPE_TWITCH_CHANNEL = "twitch_channel"

#: Default resolver instance -- stateless, safe to share (mirrors
#: `credential_resolver.py`'s own module-level convention of callers
#: constructing `DefaultCredentialResolver()` directly where no DI seam
#: is needed).
_resolver: CredentialResolver = DefaultCredentialResolver()


class TwitchRefreshRevokedError(Exception):
    """Raised when a stored refresh token was rejected by Twitch and the connection was revoked.

    The tenant's layer-2 `platform_connections` row for `platform="twitch"`
    has already been deleted by the time this is raised -- callers (the
    role-sync worker) must stop syncing for this tenant and surface a
    "Twitch needs to be reconnected" state, never retry. The tenant's
    layer-1 `tenant_platform_apps` row (its own app's `client_id`/
    `client_secret`) is untouched -- only the per-channel grant was
    compromised, not the app itself.
    """


def _app_payload(*, client_id: str, client_secret: str) -> dict[str, Any]:
    """Layer-1 app-credential payload for Twitch -- `client_id`/`client_secret` only.

    `store_tenant_credentials()` is payload-agnostic (it just encrypts
    whatever dict it's given); this is the one place Twitch's layer-1
    shape is pinned, now that the per-channel token has moved to layer 2.
    """
    return {"client_id": client_id, "client_secret": client_secret}


async def store_initial_credentials(
    dal: Any,
    *,
    tenant_id: int,
    client_id: str,
    client_secret: str,
    token: TwitchTokenResult,
    installed_by_user_id: int,
) -> None:
    """Persist a tenant's freshly-authorized Twitch app (layer 1) + channel connection (layer 2).

    Raises `services.errors.ApiError` (via `store_tenant_credentials`) if
    `tenant_id` is somehow the global tenant -- never reachable through
    `blueprints/v1/tenant_twitch_install.py`, which rejects tenant 0 before
    this is called, but kept as the DB-level backstop
    `store_tenant_credentials` already provides. Raises
    `TransportUnavailable` if the authorizing channel's Twitch user id
    cannot be resolved (`fetch_token_user_id` failure) -- the layer-1 app
    row is still persisted in that case (the app credentials themselves
    were valid), but no layer-2 connection is created; the caller must
    treat the overall install as failed and surface a retry.
    """
    store_tenant_credentials(
        dal,
        tenant_id=tenant_id,
        is_global_tenant=False,
        platform=PLATFORM_TWITCH,
        payload=_app_payload(client_id=client_id, client_secret=client_secret),
        installed_by_user_id=installed_by_user_id,
    )

    try:
        broadcaster_id = await fetch_token_user_id(
            access_token=token.access_token, client_id=client_id
        )
    except TwitchOAuthError as exc:
        logger.warning("twitch_install_credentials.resource_lookup_failed tenant_id=%s", tenant_id)
        raise TransportUnavailable(
            f"tenant {tenant_id}: could not resolve the authorized Twitch channel"
        ) from exc

    upsert_platform_connection(
        dal,
        tenant_id=tenant_id,
        platform=PLATFORM_TWITCH,
        resource_type=RESOURCE_TYPE_TWITCH_CHANNEL,
        resource_id=broadcaster_id,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        installed_by_user_id=installed_by_user_id,
    )


@dataclass(slots=True, frozen=True)
class RefreshedTwitchCredentials:
    """Result of a successful `refresh_stored_credentials()` call."""

    tenant_id: int
    access_token: str
    scopes: list[str]


async def refresh_stored_credentials(dal: Any, *, tenant_id: int) -> RefreshedTwitchCredentials:
    """Refresh a tenant's stored Twitch access token, rotating the refresh token.

    Raises `services.credential_resolver.TransportUnavailable` if the
    tenant has no stored layer-1 app credentials, or no layer-2 channel
    connection, for Twitch (never installed, or a prior revoke), or
    `TwitchRefreshRevokedError` if Twitch rejected the stored refresh
    token -- in the latter case the layer-2 connection row has already
    been deleted before this returns. Never logs a token/secret value.
    """
    resolved: PlatformCredentials = await _resolver.resolve(
        dal, tenant_id=tenant_id, is_global_tenant=False, platform=PLATFORM_TWITCH
    )
    client_id = str(resolved.payload.get("client_id", ""))
    client_secret = str(resolved.payload.get("client_secret", ""))
    if not client_id or not client_secret:
        raise TransportUnavailable(
            f"tenant {tenant_id} has an incomplete stored Twitch app credential"
        )

    connection = get_platform_connection_for_tenant(
        dal, tenant_id=tenant_id, platform=PLATFORM_TWITCH
    )
    if connection is None or not connection.refresh_token:
        raise TransportUnavailable(f"tenant {tenant_id} has no stored Twitch channel connection")

    try:
        token = await refresh_access_token(
            client_id=client_id, client_secret=client_secret, refresh_token=connection.refresh_token
        )
    except TwitchOAuthError as exc:
        logger.warning(
            "twitch_install_credentials.refresh_rejected tenant_id=%s -- revoking connection",
            tenant_id,
        )
        delete_platform_connection(dal, connection_id=connection.id)
        raise TwitchRefreshRevokedError(
            f"tenant {tenant_id}: Twitch rejected the stored refresh token; "
            "connection revoked, re-authorization required"
        ) from exc

    upsert_platform_connection(
        dal,
        tenant_id=tenant_id,
        platform=PLATFORM_TWITCH,
        resource_type=RESOURCE_TYPE_TWITCH_CHANNEL,
        resource_id=connection.resource_id,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        installed_by_user_id=connection.installed_by_user_id,
    )
    return RefreshedTwitchCredentials(
        tenant_id=tenant_id, access_token=token.access_token, scopes=token.scopes
    )
