"""`CredentialResolver` -- per-tenant platform-credential seam for Bar Citizen (migration 0035).

See `docs/CONNECTION_MODEL.local.md` for the full three-layer design.

**Foundation module.** Units C/D (Discord/Twitch OAuth bot-install flows)
and Unit F (the role-sync worker) resolve platform app credentials through
`CredentialResolver.resolve()` rather than reading env vars or
`tenant_platform_apps` directly -- one stable, typed seam, pinned
for those units to build against (mirrors `community_connections.py`'s own
"pinned, not renamed" contract precedent for other chunk-contract modules
in this port).

Two lanes, selected by `is_global_tenant` (the caller's own
`flask_core.tenancy.TenantContext.is_default`, never re-derived here):

- Tenant 0 (global/SaaS): `{PLATFORM}_CLIENT_ID`/`_CLIENT_SECRET`/
  `_BOT_TOKEN` env vars -- the same `{PLATFORM}_CLIENT_ID`/
  `_CLIENT_SECRET` naming `services/oauth_providers.py` already reads for
  this app's own SaaS-wide OAuth app, extended with an optional bot-token
  env var for platforms (Discord) that need one.
- Tenant N: decrypt `tenant_platform_apps.credentials_ciphertext`
  (AES-256-GCM via `platform_integrations_crypto`, same primitive/wire
  format `community_connections.py` uses) and JSON-decode the platform-
  defined payload -- migration 0034's own documented shape
  (`{"client_id": ..., "client_secret": ..., "bot_token": ..., "extra": {...}}`,
  still accepted as-is for Discord; Twitch's per-channel OAuth material no
  longer belongs in this payload -- see below).

Every failure mode (missing env vars, missing DB row, decrypt failure,
malformed JSON) raises the single `TransportUnavailable` error -- callers
get one exception type to handle ("this platform isn't configured for
this tenant"), never a raw `KeyError`/`PlatformCredentialCryptoError`/
`json.JSONDecodeError` leaking through the seam.

**Layers 2/3 (migration 0035, `docs/CONNECTION_MODEL.local.md`).** This
module also owns the read/write seam for `platform_connections` (layer 2
-- one row per installed resource, e.g. a Discord guild or Twitch
channel; tokens live ONLY here) and `community_connection_access` (layer
3 -- which communities may use a given connection, no tokens). Ported off
the old conflated `tenant_platform_credentials` shape (migration 0034,
which crammed a tenant's app credentials and a single resource's OAuth
tokens into one row) as this migration's own "Follow-up required" note
directs: `services/twitch_install_credentials.py` is the first real
caller, splitting its payload across layer 1 (`client_id`/`client_secret`,
via `store_tenant_credentials()` below) and layer 2
(`upsert_platform_connection()`/`get_platform_connection_for_tenant()`).
`resolve_community_connection()` is layer 3-aware (only an `approved`
grant resolves a connection) but has no caller yet in this PR -- Unit F/G
(role-sync worker's community-scoped lookups, the admin reuse/approval
UI) wire it in a follow-up; see the design doc for the full sequence.
Discord's bot-install flow (`discord_install_service.py`) is UNCHANGED in
this PR -- its bot token is a true app-level credential (one static token
for the whole Discord application, not scoped to any one guild), and the
OAuth callback it uses today never captures a `guild_id` to key a layer-2
row by in the first place; capturing one is a separate, larger rework of
that flow's own OAuth-callback contract, flagged as a follow-up in the
design doc rather than guessed at here.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.errors import bad_request, not_found
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    decrypt_value,
    encrypt_token,
)
from services.schema import bind_bar_citizen_tables

logger = logging.getLogger(__name__)


class TransportUnavailable(Exception):  # noqa: N818 - contract-pinned name, see module docstring
    """Raised when no usable credentials exist for a `(tenant_id, platform)` pair.

    Covers both lanes `resolve()` can take: tenant 0's SaaS env vars unset,
    or tenant N's DB row missing/undecryptable/malformed. Callers surface
    this as a clear "platform not configured" response rather than a raw
    decrypt or env-lookup failure.
    """


@dataclass(slots=True, frozen=True)
class PlatformCredentials:
    """Resolved, decrypted app credentials for one `(tenant_id, platform)` pair.

    `payload` is the platform-defined JSON shape (migration 0034's own
    documented convention: `client_id`/`client_secret`/`bot_token`/`extra`)
    -- callers pull whichever keys their platform integration needs.
    `source` (`"saas"` or `"tenant"`) is informational/audit only, never an
    authorization input.
    """

    tenant_id: int
    platform: str
    payload: dict[str, Any]
    source: str


class CredentialResolver(Protocol):
    """The stable interface Units C/D/F import -- one method, one typed result, one error type."""

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        """Return `platform`'s resolved credentials for `tenant_id`.

        Raises `TransportUnavailable` if none exist -- never returns a
        partially-populated result.
        """
        ...


def _saas_env_payload(platform: str) -> dict[str, Any] | None:
    """Read `{PLATFORM}_CLIENT_ID`/`_CLIENT_SECRET`/`_BOT_TOKEN` env vars for tenant 0.

    Matches `services/oauth_providers.py`'s own `client_id_env`/
    `client_secret_env` naming (`TWITCH_CLIENT_ID`, `DISCORD_CLIENT_SECRET`,
    ...) -- one source of truth for "what are this platform's SaaS
    credentials called", not a second env var map drifting from it.
    """
    prefix = platform.upper()
    client_id = os.getenv(f"{prefix}_CLIENT_ID")
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET")
    bot_token = os.getenv(f"{prefix}_BOT_TOKEN")
    if not client_id or not client_secret:
        return None
    payload: dict[str, Any] = {"client_id": client_id, "client_secret": client_secret}
    if bot_token:
        payload["bot_token"] = bot_token
    return payload


def _select_tenant_credentials_row(dal: Any, tenant_id: int, platform: str) -> Any | None:
    bind_bar_citizen_tables(dal)
    t = dal.tenant_platform_apps
    return dal((t.tenant_id == tenant_id) & (t.platform == platform)).select().first()


class DefaultCredentialResolver:
    """Default `CredentialResolver`: tenant 0 -> SaaS env vars, tenant N -> decrypted DB row."""

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        """See `CredentialResolver.resolve`."""
        if is_global_tenant:
            payload = _saas_env_payload(platform)
            if payload is None:
                raise TransportUnavailable(
                    f"no SaaS credentials configured for platform '{platform}'"
                )
            return PlatformCredentials(
                tenant_id=tenant_id, platform=platform, payload=payload, source="saas"
            )

        row = _select_tenant_credentials_row(dal, tenant_id, platform)
        if row is None:
            raise TransportUnavailable(
                f"tenant {tenant_id} has no stored credentials for platform '{platform}'"
            )

        try:
            plaintext = decrypt_value(row.credentials_ciphertext)
        except PlatformCredentialCryptoError as exc:
            logger.error(
                "credential_resolver.decrypt_failed tenant_id=%s platform=%s",
                tenant_id,
                platform,
            )
            raise TransportUnavailable(
                f"stored credentials for tenant {tenant_id} platform '{platform}' are unreadable"
            ) from exc

        try:
            payload = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise TransportUnavailable(
                f"stored credentials for tenant {tenant_id} platform '{platform}' are malformed"
            ) from exc

        return PlatformCredentials(
            tenant_id=tenant_id, platform=platform, payload=payload, source="tenant"
        )


def store_tenant_credentials(
    dal: Any,
    *,
    tenant_id: int,
    is_global_tenant: bool,
    platform: str,
    payload: dict[str, Any],
    installed_by_user_id: int | None,
) -> None:
    """Encrypt + upsert tenant N's own `(tenant_id, platform)` credentials row.

    The write side of this seam -- Units C/D's OAuth bot-install flow calls
    this once a tenant admin supplies their own app credentials. Not part
    of the `CredentialResolver` read interface itself (resolvers only
    read); kept in this module since it writes the exact row `resolve()`
    reads back. Raises `bad_request` for the global tenant -- it never
    stores a row here (also enforced by migration 0035's
    `trg_reject_global_tenant_app_credentials` DB trigger; this is a
    faster, clearer failure before hitting it).
    """
    if is_global_tenant:
        raise bad_request("the global tenant cannot store platform credentials")

    bind_bar_citizen_tables(dal)
    t = dal.tenant_platform_apps
    ciphertext = encrypt_token(json.dumps(payload))
    now = datetime.now(UTC)
    try:
        existing = dal((t.tenant_id == tenant_id) & (t.platform == platform)).select().first()
        if existing is None:
            t.insert(
                tenant_id=tenant_id,
                platform=platform,
                credentials_ciphertext=ciphertext,
                installed_by_user_id=installed_by_user_id,
                created_at=now,
                updated_at=now,
            )
        else:
            dal(t.id == existing.id).update(
                credentials_ciphertext=ciphertext,
                installed_by_user_id=installed_by_user_id,
                updated_at=now,
            )
        dal.commit()
    except Exception:
        dal.rollback()
        raise


# =============================================================================
# Layer 2/3: platform_connections + community_connection_access (migration 0035)
# =============================================================================


@dataclass(slots=True, frozen=True)
class PlatformConnection:
    """One decrypted layer-2 `platform_connections` row -- a resource install, tokens included.

    `installed_by_user_id` is this CONNECTION's own installer, independent
    of layer 1's `tenant_platform_apps.installed_by_user_id` (the app's own
    installer) -- the two can differ (e.g. a different admin re-authorizes
    one channel under an app someone else originally registered).
    """

    id: int
    tenant_id: int
    platform: str
    resource_type: str
    resource_id: str
    access_token: str
    refresh_token: str | None
    status: str
    installed_by_user_id: int | None


def _decrypt_connection_row(row: Any) -> PlatformConnection:
    """Decrypt one `platform_connections` row into a `PlatformConnection`.

    Raises `TransportUnavailable` on a decrypt failure (tamper/corrupt
    ciphertext, key mismatch) -- same fail-closed posture `resolve()`
    already uses for layer-1 rows, never surfacing ciphertext as if it
    were a usable token.
    """
    try:
        access_token = decrypt_value(row.access_token)
        refresh_token = decrypt_value(row.refresh_token) if row.refresh_token else None
    except PlatformCredentialCryptoError as exc:
        logger.error("credential_resolver.connection_decrypt_failed connection_id=%s", row.id)
        raise TransportUnavailable(f"stored connection {row.id} tokens are unreadable") from exc

    return PlatformConnection(
        id=int(row.id),
        tenant_id=int(row.tenant_id),
        platform=row.platform,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        access_token=access_token,
        refresh_token=refresh_token,
        status=row.status,
        installed_by_user_id=(int(row.installed_by_user_id) if row.installed_by_user_id else None),
    )


def upsert_platform_connection(
    dal: Any,
    *,
    tenant_id: int,
    platform: str,
    resource_type: str,
    resource_id: str,
    access_token: str,
    refresh_token: str | None,
    installed_by_user_id: int | None,
    status: str = "active",
) -> int:
    """Insert or update the one `(tenant_id, platform, resource_id)` connection row.

    Tokens are AES-256-GCM encrypted the same primitive `store_tenant_
    credentials()` uses for layer 1. Returns the connection's `id` --
    callers granting community access (layer 3, not yet wired to any
    caller in this PR -- see module docstring) pass this straight to
    `grant_community_connection_access()`.
    """
    bind_bar_citizen_tables(dal)
    t = dal.platform_connections
    now = datetime.now(UTC)
    encrypted_access = encrypt_token(access_token)
    encrypted_refresh = encrypt_token(refresh_token) if refresh_token else None
    try:
        existing = (
            dal(
                (t.tenant_id == tenant_id)
                & (t.platform == platform)
                & (t.resource_id == resource_id)
            )
            .select()
            .first()
        )
        if existing is None:
            connection_id = int(
                t.insert(
                    tenant_id=tenant_id,
                    platform=platform,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    access_token=encrypted_access,
                    refresh_token=encrypted_refresh,
                    status=status,
                    installed_by_user_id=installed_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            connection_id = int(existing.id)
            dal(t.id == connection_id).update(
                access_token=encrypted_access,
                refresh_token=encrypted_refresh,
                status=status,
                installed_by_user_id=installed_by_user_id,
                updated_at=now,
            )
        dal.commit()
        return connection_id
    except Exception:
        dal.rollback()
        raise


def get_platform_connection(
    dal: Any, *, tenant_id: int, platform: str, resource_id: str
) -> PlatformConnection | None:
    """Return the decrypted `(tenant_id, platform, resource_id)` connection, or `None`.

    Always filters by `tenant_id` -- never resolvable by `resource_id`
    alone, so one tenant can never read back another tenant's connection
    even if both happen to reference the same `resource_id` string.
    """
    bind_bar_citizen_tables(dal)
    t = dal.platform_connections
    row = (
        dal((t.tenant_id == tenant_id) & (t.platform == platform) & (t.resource_id == resource_id))
        .select()
        .first()
    )
    return _decrypt_connection_row(row) if row is not None else None


def get_platform_connection_for_tenant(
    dal: Any, *, tenant_id: int, platform: str
) -> PlatformConnection | None:
    """Return tenant's single active connection for `platform`, if one exists.

    Install flows with exactly one resource per tenant today (Twitch's
    tenant-install flow -- one channel per tenant) resolve by tenant alone
    without needing to know `resource_id` up front; a tenant with more
    than one active connection for the same platform is unsupported by
    this helper (returns the first match) -- multi-resource-per-tenant
    resolution by caller-supplied `resource_id` is `get_platform_
    connection()`'s job, and community-scoped resolution across many
    tenants' connections is `resolve_community_connection()`'s job.
    """
    bind_bar_citizen_tables(dal)
    t = dal.platform_connections
    row = (
        dal((t.tenant_id == tenant_id) & (t.platform == platform) & (t.status == "active"))
        .select()
        .first()
    )
    return _decrypt_connection_row(row) if row is not None else None


def delete_platform_connection(dal: Any, *, connection_id: int) -> None:
    """Hard-delete one `platform_connections` row and its `community_connection_access` grants.

    Postgres's own `ON DELETE CASCADE` (migration 0035) already does this
    at the DB level in production; this function ALSO deletes the grant
    rows explicitly, application-side, before the connection row -- pydal
    uses plain `integer`/`bigint` FK-shaped columns here (this file's own
    "FK-shaped columns, not pydal `reference` fields" convention, see
    `bind_bar_citizen_tables()`'s docstring), so sqlite test DBs (no real
    FK, no cascade) would otherwise leave an orphaned grant behind -- this
    keeps both backends' observable behavior identical rather than
    silently relying on a cascade sqlite can't express.
    """
    bind_bar_citizen_tables(dal)
    try:
        dal(dal.community_connection_access.connection_id == connection_id).delete()
        dal(dal.platform_connections.id == connection_id).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise


def grant_community_connection_access(
    dal: Any,
    *,
    community_id: int,
    connection_id: int,
    status: str = "approved",
    requested_by_user_id: int | None = None,
    approved_by_user_id: int | None = None,
) -> int:
    """Insert or update the one `(community_id, connection_id)` grant row. Returns its `id`.

    No tokens/credentials here at all (layer 3) -- purely which community
    may use `connection_id`, and under what approval state. The first
    community to install a resource is expected to call this with
    `status="approved"` immediately (its own install IS the approval); a
    second community reusing the same connection calls this with the
    default `status="pending"`, requiring a later call with
    `status="approved"` + `approved_by_user_id` set from a server/guild
    admin action before `resolve_community_connection()` will return it.

    Raises `bad_request` if `community_id`'s own tenant does not match
    `connection_id`'s tenant -- a community may only ever be granted
    access to a connection installed under its OWN tenant's app; this is
    the actual cross-tenant guard (`resolve_community_connection()`'s
    isolation depends on every grant row satisfying it, not on a query
    filter alone).
    """
    bind_bar_citizen_tables(dal)
    # Explicit column lists, never a bare `.select()`/`ALL` -- this repo has hit
    # pydal-vs-Postgres column drift before (`community_connections.py`'s own module
    # docstring); `communities` in particular carries many more production columns
    # than this function needs, so naming exactly `id`/`tenant_id` here also means
    # this query only ever depends on those two columns actually existing.
    community = (
        dal(dal.communities.id == community_id)
        .select(dal.communities.id, dal.communities.tenant_id)
        .first()
    )
    if community is None:
        raise not_found(f"community {community_id} does not exist")
    connection = (
        dal(dal.platform_connections.id == connection_id)
        .select(dal.platform_connections.id, dal.platform_connections.tenant_id)
        .first()
    )
    if connection is None:
        raise not_found(f"connection {connection_id} does not exist")
    if int(community.tenant_id) != int(connection.tenant_id):
        raise bad_request(
            f"community {community_id} (tenant {community.tenant_id}) cannot be granted "
            f"access to connection {connection_id} (tenant {connection.tenant_id})"
        )

    t = dal.community_connection_access
    now = datetime.now(UTC)
    try:
        existing = (
            dal((t.community_id == community_id) & (t.connection_id == connection_id))
            .select()
            .first()
        )
        if existing is None:
            grant_id = int(
                t.insert(
                    community_id=community_id,
                    connection_id=connection_id,
                    status=status,
                    requested_by_user_id=requested_by_user_id,
                    approved_by_user_id=approved_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            grant_id = int(existing.id)
            dal(t.id == grant_id).update(
                status=status,
                approved_by_user_id=approved_by_user_id,
                updated_at=now,
            )
        dal.commit()
        return grant_id
    except Exception:
        dal.rollback()
        raise


def resolve_community_connection(
    dal: Any, *, community_id: int, platform: str
) -> PlatformConnection | None:
    """Return `community_id`'s one APPROVED, ACTIVE connection for `platform`, else `None`.

    Layer 3-aware: only a `community_connection_access` row with
    `status="approved"` ever resolves a connection here -- a `pending` or
    `revoked` grant, or a connection whose own `status` isn't `"active"`,
    both resolve to `None`, same fail-closed posture the rest of this
    module uses. Tenant isolation falls out structurally: a community's
    grants only ever reference connections through its own `community_id`
    (never a bare `resource_id`/tenant lookup), so this can never return a
    different tenant's connection for a community it doesn't belong to.
    """
    bind_bar_citizen_tables(dal)
    access = dal.community_connection_access
    grants = dal((access.community_id == community_id) & (access.status == "approved")).select(
        access.connection_id
    )
    connection_ids = {int(grant.connection_id) for grant in grants}
    if not connection_ids:
        return None

    conn = dal.platform_connections
    row = (
        dal(
            conn.id.belongs(connection_ids)
            & (conn.platform == platform)
            & (conn.status == "active")
        )
        .select()
        .first()
    )
    return _decrypt_connection_row(row) if row is not None else None
