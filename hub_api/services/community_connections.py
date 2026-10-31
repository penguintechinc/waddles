"""Per-community OAuth "Connections" service layer (gh-320, chunk C1).

One `platform_integrations` row per `(community_id, platform)` with
`integration_type='community_oauth'` (`user_id` NULL) is this feature's
storage lane -- a lane `credential_manager_module/services/
refresh_service.py`'s `bot`/`user_oauth` writers never touch, so there is
no write-write overlap with that owner (see `platform_integrations_crypto.
py`'s module docstring). Tables are bound lazily via
`services.schema.bind_platform_tables()`, the same idempotent
guarded-membership-check pattern every other hub_api service module in
this port follows (see `community_loyalty.py`'s own module docstring).

Every function is tenant-scoped by `community_id` alone -- the caller
(blueprint layer, chunk C3) has already resolved and authorized
`community_id` against the token's tenant, same trust boundary this
port's other service modules assume (see `community_loyalty.py`'s own
module docstring).

This module never touches `platform_integrations` directly: `waddles_hub_api` is denied the table
(H-3) and every read/write goes through `services.platform_integrations_store` -- on Postgres the
narrow SECURITY DEFINER functions of migration 0069 (gh-969), community_oauth lane and ciphertext
only; the module holds no path to operator bot credentials.

Access/refresh tokens are AES-256-GCM encrypted at rest via
`provider_connections.vault.seal_secret()`/`open_secret()` (AES-256-GCM, AAD-bound to
`community_id:provider` -- finding M4) --
`ConnectionStatus` (the DTO handed back to the blueprint/webui layer,
chunks C3/C4) never carries token material, only connection metadata.
`DecryptedTokens` is for internal callers only (chunks C5/C6's
provider-API consumers) and must never be serialized to a public route.

`SUPPORTED_PROVIDERS`, `ConnectionStatus`, `DecryptedTokens`, and every
function signature below (including `UnsupportedProvider`'s exact name,
despite ruff N818's `*Error`-suffix convention) are the gh-320 chunk
contract other in-flight chunks (C2 providers registry, C3 blueprint, C4
webui, C5/C6 consumers) code against directly -- pinned, not renamed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services import platform_integrations_store as store
from services.errors import ApiError, bad_request, conflict, not_found
from services.platform_integrations_crypto import PlatformCredentialCryptoError, connection_aad
from services.provider_connections.telemetry import log_failure
from services.provider_connections.vault import (
    SEAL_AAD,
    SEAL_LEGACY,
    open_secret,
    reseal_if_legacy,
    seal_secret,
)

logger = logging.getLogger(__name__)

#: Explicit marker for community-bound callers with no request tenant (machine JWT, CronJob):
#: the tenant is resolved from the community row and passed to SQL -- there is NO default.
SYSTEM = store.Scope.SYSTEM


#: Providers this feature supports -- `list_connections()` always returns
#: exactly one entry per provider, in this order.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("youtube", "spotify", "twitch", "discord", "kick", "slack")


class UnsupportedProvider(ValueError):  # noqa: N818 - contract-pinned name, see module docstring
    """Raised when `provider` is not one of `SUPPORTED_PROVIDERS`."""


@dataclass(slots=True)
class ConnectionStatus:
    """One provider's connection state for a community -- never carries token material."""

    provider: str
    connected: bool
    scopes: list[str]
    expires_at: str | None
    updated_at: str | None
    account_label: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire-shape dict for JSON responses/webui consumption."""
        return {
            "provider": self.provider,
            "connected": self.connected,
            "scopes": self.scopes,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "account_label": self.account_label,
        }


@dataclass(slots=True)
class DecryptedTokens:
    """Decrypted OAuth material for one connection -- internal use only, never serialized."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


# ---------------------------------------------------------------------------
# Validation / table binding
# ---------------------------------------------------------------------------


def _validate_community(community_id: int) -> None:
    if community_id <= 0:
        raise bad_request("community_id must be a positive integer")


def _validate(community_id: int, provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedProvider(f"unsupported provider '{provider}'")
    _validate_community(community_id)


# ---------------------------------------------------------------------------
# DTO conversion
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _account_label(row: store.StatusRow) -> str | None:
    config_data = row.config_data
    if isinstance(config_data, dict):
        label = config_data.get("account_label")
        return str(label) if label else None
    return None


def _status_from_row(provider: str, row: store.StatusRow) -> ConnectionStatus:
    return ConnectionStatus(
        provider=provider,
        connected=True,
        scopes=list(row.scopes) if row.scopes else [],
        expires_at=_iso(row.expires_at),
        updated_at=_iso(row.updated_at),
        account_label=_account_label(row),
    )


def _disconnected_status(provider: str) -> ConnectionStatus:
    return ConnectionStatus(
        provider=provider,
        connected=False,
        scopes=[],
        expires_at=None,
        updated_at=None,
        account_label=None,
    )


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------

#: Bounded retries for compare-and-set re-seals that lose a race with another writer.
_CAS_ATTEMPTS = 3


async def _run[T](db: Any, fn: Callable[[Any], T]) -> T:
    """Run a synchronous store call on the DAL's executor thread (never on the event loop)."""
    loop = asyncio.get_running_loop()
    dal = db.dal
    return await loop.run_in_executor(db.executor, lambda: fn(dal))


def _rejected(exc: store.StoreRejectedError) -> ApiError:
    """Map a definer-function refusal to an `ApiError` that reveals nothing about other tenants."""
    if exc.outcome == "forbidden_tenant":
        return not_found("Community not found")
    if exc.outcome == "seal_downgrade":
        return conflict("connection is sealed with a newer scheme; refusing a downgrade")
    return bad_request("invalid connection request")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_connections(
    db: Any, community_id: int, *, tenant_id: store.TenantScope
) -> list[ConnectionStatus]:
    """Return one `ConnectionStatus` per `SUPPORTED_PROVIDERS`, `connected=False` when unset.

    `community_id` MUST come from the validated request scope; `tenant_id` (the validated tenant,
    REQUIRED -- or the explicit `Scope.SYSTEM` for community-bound machine/CronJob callers) makes
    the DB reject a foreign community (defence in depth).
    """
    _validate_community(community_id)
    rows = await _run(
        db, lambda dal: store.community_status(dal, community_id, None, tenant_id=tenant_id)
    )
    by_provider = {row.platform: row for row in rows}
    logger.debug("community_connections.list community_id=%s", community_id)
    return [
        _status_from_row(p, by_provider[p]) if p in by_provider else _disconnected_status(p)
        for p in SUPPORTED_PROVIDERS
    ]


async def get_connection(
    db: Any, community_id: int, provider: str, *, tenant_id: store.TenantScope
) -> ConnectionStatus | None:
    """Return `provider`'s connection status, or `None` if there's no active connection."""
    _validate(community_id, provider)
    rows = await _run(
        db, lambda dal: store.community_status(dal, community_id, provider, tenant_id=tenant_id)
    )
    row = rows[0] if rows else None
    logger.debug(
        "community_connections.get community_id=%s provider=%s found=%s",
        community_id,
        provider,
        row is not None,
    )
    return _status_from_row(provider, row) if row is not None else None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


async def upsert_connection(
    db: Any,
    community_id: int,
    provider: str,
    *,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: list[str],
    account_label: str | None,
    actor_user_id: int | None,
    tenant_id: store.TenantScope,
) -> ConnectionStatus:
    """Insert or update the single active `(community_id, provider)` row; sets `is_active=True`.

    `actor_user_id` is accepted for contract compatibility but is NOT persisted -- the DB cannot
    verify an end-user identity, so ownership columns are never written from it (gh-969).
    """
    _validate(community_id, provider)
    if not access_token or not access_token.strip():
        raise bad_request("access_token is required")

    aad = connection_aad(community_id=community_id, provider=provider)
    encrypted_access = seal_secret(access_token, aad=aad)
    encrypted_refresh = seal_secret(refresh_token, aad=aad) if refresh_token else None
    try:
        row = await _run(
            db,
            lambda dal: store.upsert(
                dal,
                community_id=community_id,
                platform=provider,
                access_token=encrypted_access,
                refresh_token=encrypted_refresh,
                expires_at=expires_at,
                scopes=list(scopes),
                account_label=account_label,
                seal_version=SEAL_AAD,
                tenant_id=tenant_id,
            ),
        )
    except store.StoreRejectedError as exc:
        raise _rejected(exc) from exc
    logger.debug("community_connections.upsert community_id=%s provider=%s", community_id, provider)
    return _status_from_row(provider, row)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete_connection(
    db: Any,
    community_id: int,
    provider: str,
    actor_user_id: int | None,
    *,
    tenant_id: store.TenantScope,
) -> bool:
    """Deactivate + null out tokens for the active `(community_id, provider)` row.

    Returns `False` (no-op) if there was nothing active to delete. `actor_user_id` is accepted
    for contract compatibility but not persisted (see `upsert_connection`).
    """
    _validate(community_id, provider)
    try:
        deleted = await _run(
            db,
            lambda dal: store.disconnect(
                dal, community_id=community_id, platform=provider, tenant_id=tenant_id
            ),
        )
    except store.StoreRejectedError as exc:
        raise _rejected(exc) from exc
    logger.debug(
        "community_connections.delete community_id=%s provider=%s deleted=%s",
        community_id,
        provider,
        deleted,
    )
    return deleted


def _log_suspected_downgrade(community_id: int, provider: str) -> None:
    """Record (ids only) a row whose column says legacy while its ciphertext is AAD-bound."""
    logger.warning(
        "community_connections.suspected_seal_downgrade community_id=%s provider=%s "
        "-- ciphertext is AAD-bound but seal_version says legacy; not re-sealed",
        community_id,
        provider,
    )


def _refresh_for_stamp(
    row: store.SecretRow, aad: bytes, *, community_id: int, provider: str
) -> str | None:
    """The refresh-token value to write when the row is stamped AAD-bound, `None` = leave as is.

    Every stamped row must hold a refresh token that really is AAD-sealed: plaintext (an
    `is_encrypted = FALSE` row) is sealed now, a genuinely legacy ciphertext is re-sealed, and a
    ciphertext that already opens under the AAD is kept (and flagged if the column claimed legacy).
    """
    if not row.refresh_token:
        return None
    if not row.is_encrypted:
        return seal_secret(row.refresh_token, aad=aad)
    if int(row.seal_version or SEAL_LEGACY) != SEAL_LEGACY:
        return None
    resealed = reseal_if_legacy(row.refresh_token, aad=aad)
    if resealed is None:
        _log_suspected_downgrade(community_id, provider)
    return resealed


def _reseal_legacy(
    dal: Any,
    row: store.SecretRow,
    *,
    community_id: int,
    provider: str,
    tenant_id: store.TenantScope,
) -> bool:
    """If `row` is still legacy (v0), re-seal both tokens as AAD-bound (v1) via compare-and-set."""
    if not row.is_encrypted or int(row.seal_version or SEAL_LEGACY) != SEAL_LEGACY:
        return False
    if not row.access_token:
        return False
    aad = connection_aad(community_id=community_id, provider=provider)
    access = reseal_if_legacy(row.access_token, aad=aad)
    refresh = reseal_if_legacy(row.refresh_token, aad=aad) if row.refresh_token else None
    if access is None or (row.refresh_token and refresh is None):
        # Opens under the AAD although the column says legacy: a suspected downgrade. Never
        # re-bless it silently -- leave the row for an operator.
        _log_suspected_downgrade(community_id, provider)
        return False
    return store.set_sealed(
        dal,
        community_id=community_id,
        platform=provider,
        expected_access=row.access_token,
        access_token=access,
        refresh_token=refresh,
        expires_at=None,
        seal_version=SEAL_AAD,
        touch_updated_at=False,
        tenant_id=tenant_id,
    )


async def _reseal_legacy_row(
    db: Any, row: store.SecretRow, *, community_id: int, provider: str, tenant_id: store.TenantScope
) -> None:
    """Lazy M4 upgrade on read: best effort -- a failure never breaks the read that triggered it.

    The row was just opened successfully, so a failure here is operational (lock/connection), not
    tamper; it is logged by type only and `scripts/reseal_connections.py` reports any leftovers.
    """
    try:
        await _run(
            db,
            lambda dal: _reseal_legacy(
                dal, row, community_id=community_id, provider=provider, tenant_id=tenant_id
            ),
        )
    except Exception as exc:  # noqa: BLE001 - see docstring: reseal is an opportunistic upgrade
        log_failure(
            logger,
            "community_connections.lazy_reseal_failed",
            exc,
            level=logging.WARNING,
            community_id=community_id,
            provider=provider,
        )


# ---------------------------------------------------------------------------
# Decrypted token access (internal use only)
# ---------------------------------------------------------------------------


async def get_decrypted_tokens(
    db: Any, community_id: int, provider: str, *, tenant_id: store.TenantScope
) -> DecryptedTokens | None:
    """Return decrypted OAuth material for provider-API callers; `None` if not connected.

    A decrypt failure on a row that claims to be encrypted (tamper/corrupt
    ciphertext, key mismatch) is logged and re-raised as
    `PlatformCredentialCryptoError` -- unlike `platform_integrations_crypto.
    decrypt_if_needed()`'s test-connection fallback, a caller here is about
    to make a real provider API call and must not silently receive
    ciphertext as if it were a usable token.
    """
    _validate(community_id, provider)
    row = await _run(
        db, lambda dal: store.get_secret(dal, community_id, provider, tenant_id=tenant_id)
    )
    if row is None or not row.access_token:
        return None

    aad = connection_aad(community_id=community_id, provider=provider)
    seal_version = int(row.seal_version or SEAL_LEGACY)
    try:
        if row.is_encrypted:
            access_token = open_secret(row.access_token, aad=aad, seal_version=seal_version)
        else:
            access_token = row.access_token
        refresh_token = None
        if row.refresh_token:
            refresh_token = (
                open_secret(row.refresh_token, aad=aad, seal_version=seal_version)
                if row.is_encrypted
                else row.refresh_token
            )
    except PlatformCredentialCryptoError:
        logger.error(
            "community_connections.decrypt_failed community_id=%s provider=%s",
            community_id,
            provider,
        )
        raise

    if row.is_encrypted and seal_version == SEAL_LEGACY:
        await _reseal_legacy_row(
            db, row, community_id=community_id, provider=provider, tenant_id=tenant_id
        )

    return DecryptedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=row.expires_at if isinstance(row.expires_at, datetime) else None,
        scopes=list(row.scopes) if row.scopes else [],
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _store_refreshed(
    dal: Any,
    *,
    community_id: int,
    provider: str,
    access_token: str,
    expires_at: datetime | None,
    tenant_id: store.TenantScope,
) -> bool:
    """Seal `access_token` over the current row via compare-and-set; `False` = no active row."""
    aad = connection_aad(community_id=community_id, provider=provider)
    for _ in range(_CAS_ATTEMPTS):
        row = store.get_secret(dal, community_id, provider, tenant_id=tenant_id)
        if row is None or not row.access_token:
            return False
        # The row is about to be stamped AAD-bound/encrypted, so its untouched refresh token must
        # really be AAD-sealed in the same write or it would mislabel (or fail) on next read.
        refresh = _refresh_for_stamp(row, aad, community_id=community_id, provider=provider)
        if store.set_sealed(
            dal,
            community_id=community_id,
            platform=provider,
            expected_access=row.access_token,
            access_token=seal_secret(access_token, aad=aad),
            refresh_token=refresh,
            expires_at=expires_at,
            seal_version=SEAL_AAD,
            touch_updated_at=True,
            tenant_id=tenant_id,
        ):
            return True
    raise RuntimeError(
        f"community_connections.refresh_cas_exhausted community_id={community_id} "
        f"provider={provider}"
    )


async def store_refreshed_access_token(
    db: Any,
    community_id: int,
    provider: str,
    *,
    access_token: str,
    expires_at: datetime | None,
    tenant_id: store.TenantScope,
) -> None:
    """Overwrite the active connection's encrypted `access_token`/`expires_at` after a refresh.

    Raises `not_found` if there is no active `(community_id, provider)`
    connection to refresh -- a refresh implies a connection already
    existed; this never creates a new row.
    """
    _validate(community_id, provider)
    if not access_token or not access_token.strip():
        raise bad_request("access_token is required")

    try:
        found = await _run(
            db,
            lambda dal: _store_refreshed(
                dal,
                community_id=community_id,
                provider=provider,
                access_token=access_token,
                expires_at=expires_at,
                tenant_id=tenant_id,
            ),
        )
    except store.StoreRejectedError as exc:
        raise _rejected(exc) from exc
    if not found:
        raise not_found(f"no active {provider} connection for community {community_id}")
    logger.debug(
        "community_connections.refresh_stored community_id=%s provider=%s", community_id, provider
    )
