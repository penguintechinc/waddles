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

Every `platform_integrations` select names its columns explicitly
(`_fields()`) rather than a bare `.select()`/`ALL` -- this repo has hit
pydal-vs-Postgres column drift before (a drifted/renamed column should
fail loudly at the call site, not silently resolve to whatever ALL
currently returns).

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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.errors import bad_request, not_found
from services.platform_integrations_crypto import PlatformCredentialCryptoError, connection_aad
from services.provider_connections.telemetry import log_failure
from services.provider_connections.vault import (
    SEAL_AAD,
    SEAL_LEGACY,
    open_secret,
    reseal_if_legacy,
    seal_secret,
)
from services.schema import bind_platform_tables

logger = logging.getLogger(__name__)

#: Providers this feature supports -- `list_connections()` always returns
#: exactly one entry per provider, in this order.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("youtube", "spotify", "twitch", "discord", "kick", "slack")

#: This feature's fixed `platform_integrations.integration_type` value.
_INTEGRATION_TYPE = "community_oauth"

#: OAuth token *type* label written to `platform_integrations.token_type` --
#: not a secret (ruff S106 false-positives on the literal "Bearer" here).
_DEFAULT_TOKEN_TYPE = "Bearer"  # noqa: S105

#: Explicit `platform_integrations` column list for every select -- see
#: module docstring on why this is never a bare `.select()`.
_SELECT_COLUMNS: tuple[str, ...] = (
    "id",
    "platform",
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "token_type",
    "expires_at",
    "scopes",
    "config_data",
    "is_active",
    "is_encrypted",
    "seal_version",
    "created_at",
    "updated_at",
)


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


def _ensure_tables(dal: Any) -> None:
    """Idempotently bind `platform_integrations` (+ its M3 group) -- cheap membership check."""
    bind_platform_tables(dal)


def _for_update(dal: Any) -> bool:
    """`SELECT ... FOR UPDATE` on every adapter except sqlite (rejects the syntax outright)."""
    return bool(dal._adapter.dbengine != "sqlite")


def _fields(dal: Any) -> list[Any]:
    """Explicit `Field` objects for `_SELECT_COLUMNS`, in order."""
    table = dal.platform_integrations
    return [getattr(table, name) for name in _SELECT_COLUMNS]


def _active_query(dal: Any, community_id: int, provider: str) -> Any:
    t = dal.platform_integrations
    return (
        (t.community_id == community_id)
        & (t.platform == provider)
        & (t.integration_type == _INTEGRATION_TYPE)
        & (t.is_active == True)  # noqa: E712 - pydal idiom
    )


# ---------------------------------------------------------------------------
# DTO conversion
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _account_label(row: Any) -> str | None:
    config_data = row.config_data
    if isinstance(config_data, dict):
        label = config_data.get("account_label")
        return str(label) if label else None
    return None


def _status_from_row(provider: str, row: Any) -> ConnectionStatus:
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
# Reads
# ---------------------------------------------------------------------------


async def _select_one(db: Any, dal: Any, community_id: int, provider: str) -> Any | None:
    query = _active_query(dal, community_id, provider)
    rows = await db.select_async(dal(query), *_fields(dal))
    return rows.first() if rows else None


async def list_connections(db: Any, community_id: int) -> list[ConnectionStatus]:
    """Return one `ConnectionStatus` per `SUPPORTED_PROVIDERS`, `connected=False` when unset."""
    _validate_community(community_id)
    dal = db.dal
    _ensure_tables(dal)
    t = dal.platform_integrations
    query = (
        (t.community_id == community_id)
        & (t.integration_type == _INTEGRATION_TYPE)
        & (t.is_active == True)  # noqa: E712 - pydal idiom
    )
    rows = await db.select_async(dal(query), *_fields(dal))
    by_provider = {row.platform: row for row in rows}
    logger.debug("community_connections.list community_id=%s", community_id)
    return [
        _status_from_row(p, by_provider[p]) if p in by_provider else _disconnected_status(p)
        for p in SUPPORTED_PROVIDERS
    ]


async def get_connection(db: Any, community_id: int, provider: str) -> ConnectionStatus | None:
    """Return `provider`'s connection status, or `None` if there's no active connection."""
    _validate(community_id, provider)
    dal = db.dal
    _ensure_tables(dal)
    row = await _select_one(db, dal, community_id, provider)
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


def _sync_upsert(
    dal: Any,
    *,
    community_id: int,
    provider: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: list[str],
    account_label: str | None,
    actor_user_id: int | None,
    now: datetime,
) -> Any:
    """Single executor job: lock the row (if any), encrypt tokens, insert-or-update, commit."""
    for_update = _for_update(dal)
    t = dal.platform_integrations
    try:
        query = (
            (t.community_id == community_id)
            & (t.platform == provider)
            & (t.integration_type == _INTEGRATION_TYPE)
        )
        existing = dal(query).select(*_fields(dal), for_update=for_update).first()

        aad = connection_aad(community_id=community_id, provider=provider)
        encrypted_access = seal_secret(access_token, aad=aad)
        encrypted_refresh = seal_secret(refresh_token, aad=aad) if refresh_token else None
        config_data = {"account_label": account_label}

        if existing is None:
            row_id = int(
                t.insert(
                    platform=provider,
                    integration_type=_INTEGRATION_TYPE,
                    community_id=community_id,
                    user_id=None,
                    access_token=encrypted_access,
                    refresh_token=encrypted_refresh,
                    client_id=None,
                    client_secret=None,
                    token_type=_DEFAULT_TOKEN_TYPE,
                    expires_at=expires_at,
                    scopes=scopes,
                    config_data=config_data,
                    is_active=True,
                    is_encrypted=True,
                    seal_version=SEAL_AAD,
                    created_at=now,
                    updated_at=now,
                    created_by_user_id=actor_user_id,
                    updated_by_user_id=actor_user_id,
                )
            )
        else:
            row_id = int(existing.id)
            dal(t.id == row_id).update(
                access_token=encrypted_access,
                refresh_token=encrypted_refresh,
                token_type=_DEFAULT_TOKEN_TYPE,
                expires_at=expires_at,
                scopes=scopes,
                config_data=config_data,
                is_active=True,
                is_encrypted=True,
                seal_version=SEAL_AAD,
                updated_at=now,
                updated_by_user_id=actor_user_id,
            )

        refreshed = dal(t.id == row_id).select(*_fields(dal)).first()
        dal.commit()
        return refreshed
    except Exception:
        dal.rollback()
        raise


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
) -> ConnectionStatus:
    """Insert or update the single active `(community_id, provider)` row; sets `is_active=True`."""
    _validate(community_id, provider)
    if not access_token or not access_token.strip():
        raise bad_request("access_token is required")

    dal = db.dal
    _ensure_tables(dal)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    row = await loop.run_in_executor(
        db.executor,
        lambda: _sync_upsert(
            dal,
            community_id=community_id,
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=list(scopes),
            account_label=account_label,
            actor_user_id=actor_user_id,
            now=now,
        ),
    )
    logger.debug("community_connections.upsert community_id=%s provider=%s", community_id, provider)
    return _status_from_row(provider, row)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def _sync_delete(
    dal: Any, *, community_id: int, provider: str, actor_user_id: int | None, now: datetime
) -> bool:
    for_update = _for_update(dal)
    t = dal.platform_integrations
    try:
        query = _active_query(dal, community_id, provider)
        row = dal(query).select(*_fields(dal), for_update=for_update).first()
        if row is None:
            dal.commit()
            return False

        dal(t.id == row.id).update(
            is_active=False,
            access_token=None,
            refresh_token=None,
            client_id=None,
            client_secret=None,
            scopes=[],
            config_data=None,
            updated_at=now,
            updated_by_user_id=actor_user_id,
        )
        dal.commit()
        return True
    except Exception:
        dal.rollback()
        raise


async def delete_connection(
    db: Any, community_id: int, provider: str, actor_user_id: int | None
) -> bool:
    """Deactivate + null out tokens for the active `(community_id, provider)` row.

    Returns `False` (no-op) if there was nothing active to delete.
    """
    _validate(community_id, provider)
    dal = db.dal
    _ensure_tables(dal)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    deleted = await loop.run_in_executor(
        db.executor,
        lambda: _sync_delete(
            dal, community_id=community_id, provider=provider, actor_user_id=actor_user_id, now=now
        ),
    )
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


def _refresh_for_stamp(row: Any, aad: bytes, *, community_id: int, provider: str) -> str | None:
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


def _sync_reseal_legacy(dal: Any, *, community_id: int, provider: str) -> bool:
    """Lock the active row and, if still legacy (v0), re-seal both tokens as AAD-bound (v1)."""
    for_update = _for_update(dal)
    t = dal.platform_integrations
    try:
        row = (
            dal(_active_query(dal, community_id, provider))
            .select(*_fields(dal), for_update=for_update)
            .first()
        )
        if (
            row is None
            or not row.is_encrypted
            or int(row.seal_version or SEAL_LEGACY) != SEAL_LEGACY
        ):
            dal.commit()
            return False
        aad = connection_aad(community_id=community_id, provider=provider)
        access = reseal_if_legacy(row.access_token, aad=aad)
        refresh = reseal_if_legacy(row.refresh_token, aad=aad) if row.refresh_token else None
        if access is None or (row.refresh_token and refresh is None):
            # Opens under the AAD although the column says legacy: a suspected downgrade. Never
            # re-bless it silently -- leave the row for an operator.
            _log_suspected_downgrade(community_id, provider)
            dal.commit()
            return False
        dal(t.id == row.id).update(
            access_token=access, refresh_token=refresh, seal_version=SEAL_AAD
        )
        dal.commit()
        return True
    except Exception:
        dal.rollback()
        raise


async def _reseal_legacy_row(db: Any, dal: Any, *, community_id: int, provider: str) -> None:
    """Lazy M4 upgrade on read: best effort -- a failure never breaks the read that triggered it.

    The row was just opened successfully, so a failure here is operational (lock/connection), not
    tamper; it is logged by type only and `scripts/reseal_connections.py` reports any leftovers.
    """
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            db.executor,
            lambda: _sync_reseal_legacy(dal, community_id=community_id, provider=provider),
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


async def get_decrypted_tokens(db: Any, community_id: int, provider: str) -> DecryptedTokens | None:
    """Return decrypted OAuth material for provider-API callers; `None` if not connected.

    A decrypt failure on a row that claims to be encrypted (tamper/corrupt
    ciphertext, key mismatch) is logged and re-raised as
    `PlatformCredentialCryptoError` -- unlike `platform_integrations_crypto.
    decrypt_if_needed()`'s test-connection fallback, a caller here is about
    to make a real provider API call and must not silently receive
    ciphertext as if it were a usable token.
    """
    _validate(community_id, provider)
    dal = db.dal
    _ensure_tables(dal)
    row = await _select_one(db, dal, community_id, provider)
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
        await _reseal_legacy_row(db, dal, community_id=community_id, provider=provider)

    return DecryptedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=row.expires_at if isinstance(row.expires_at, datetime) else None,
        scopes=list(row.scopes) if row.scopes else [],
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _sync_store_refreshed(
    dal: Any,
    *,
    community_id: int,
    provider: str,
    access_token: str,
    expires_at: datetime | None,
    now: datetime,
) -> bool:
    for_update = _for_update(dal)
    t = dal.platform_integrations
    try:
        query = _active_query(dal, community_id, provider)
        row = dal(query).select(*_fields(dal), for_update=for_update).first()
        if row is None:
            dal.commit()
            return False

        aad = connection_aad(community_id=community_id, provider=provider)
        refresh_values: dict[str, Any] = {}
        # The row is about to be stamped AAD-bound/encrypted, so its untouched refresh token must
        # really be AAD-sealed in the same statement or it would mislabel (or fail) on next read.
        refresh = _refresh_for_stamp(row, aad, community_id=community_id, provider=provider)
        if refresh is not None:
            refresh_values["refresh_token"] = refresh
        dal(t.id == row.id).update(
            access_token=seal_secret(access_token, aad=aad),
            expires_at=expires_at,
            is_encrypted=True,
            seal_version=SEAL_AAD,
            updated_at=now,
            **refresh_values,
        )
        dal.commit()
        return True
    except Exception:
        dal.rollback()
        raise


async def store_refreshed_access_token(
    db: Any, community_id: int, provider: str, *, access_token: str, expires_at: datetime | None
) -> None:
    """Overwrite the active connection's encrypted `access_token`/`expires_at` after a refresh.

    Raises `not_found` if there is no active `(community_id, provider)`
    connection to refresh -- a refresh implies a connection already
    existed; this never creates a new row.
    """
    _validate(community_id, provider)
    if not access_token or not access_token.strip():
        raise bad_request("access_token is required")

    dal = db.dal
    _ensure_tables(dal)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    found = await loop.run_in_executor(
        db.executor,
        lambda: _sync_store_refreshed(
            dal,
            community_id=community_id,
            provider=provider,
            access_token=access_token,
            expires_at=expires_at,
            now=now,
        ),
    )
    if not found:
        raise not_found(f"no active {provider} connection for community {community_id}")
    logger.debug(
        "community_connections.refresh_stored community_id=%s provider=%s", community_id, provider
    )
