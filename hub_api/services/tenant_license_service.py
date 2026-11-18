"""Per-tenant license key store (issue #901): sealed key, fingerprint, status, audit.

A tenant's license key is the credential the pool provider POSTs to the license server to learn
THAT tenant's team-axis pool. It is sealed with the repo's one versioned vault
(`services.provider_connections.vault`, AAD-bound to the tenant id, the same pattern as
`services/dsar_crypto.py`), stored in `tenant_license_keys` (alembic 0127) and NEVER returned,
logged or audited in plaintext: the only places it exists decrypted are
`get_tenant_license_key()`'s return value (consumed by the license-pool provider) and the local
variable inside `set_tenant_license_key()` before sealing.

`get_tenant_license_key` is the real `license_key_resolver` for
`flask_core.license_pool.LicenseServerPoolProvider`: an unreadable key (vault open failure, bad
seal version) logs the exception TYPE at ERROR and returns None so the provider fails loud with
``PoolUnavailable('no_tenant_license')``; infrastructure errors (DB) propagate and are treated by
the provider as transient. Setting, replacing or clearing a key also evicts the tenant's cached
``license_server`` pool rows (never ``operator_set`` rows) in the same transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from services import community_tier_telemetry as telemetry
from services.audit_events import (
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
)
from services.errors import ApiError, bad_request
from services.platform_integrations_crypto import PlatformCredentialCryptoError
from services.provider_connections import vault
from services.schema import bind_community_tier_tables, bind_tenant_license_key_tables

logger = logging.getLogger(__name__)

#: Printable, whitespace-free token of a plausible license-key length.
_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{15,255}$")
_AAD_PREFIX: Final[str] = "tenant-license-key:"


#: Pool-store source code of license-server-cached rows (evicted on any key change).
_POOL_SOURCE_LICENSE_SERVER: Final[int] = 1
_AXIS_TEAM: Final[int] = 3
#: Seal versions < 1 select the unauthenticated (no-AAD) open path; never valid for this table.
_MIN_SEAL_VERSION: Final[int] = 1


@dataclass(slots=True, frozen=True)
class LicenseStatus:
    """What may leave this module about a tenant's key: never the key, never the ciphertext."""

    configured: bool
    fingerprint: str | None
    set_at: datetime | None


def _aad(tenant_id: int) -> bytes:
    """Bind the ciphertext to its tenant so a blob copied to another tenant's row fails to open."""
    return f"{_AAD_PREFIX}{tenant_id}".encode("ascii")


def fingerprint_of(license_key: str) -> str:
    """Non-secret display/audit fingerprint: first 8 hex chars of SHA-256(key)."""
    return hashlib.sha256(license_key.encode("utf-8")).hexdigest()[:8]


def validate_license_key(value: object) -> str:
    """Return the stripped key or raise a 400 `ApiError` (the value is never echoed)."""
    if not isinstance(value, str) or not _KEY_RE.match(value.strip()):
        raise ApiError(
            "licenseKey must be 16-256 characters of letters, digits and . _ : -",
            400,
            "INVALID_LICENSE_KEY",
        )
    return value.strip()


def _aware(value: datetime | None) -> datetime | None:
    """Treat naive datetimes (sqlite) as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _run(async_dal: Any, job: Any) -> Any:
    """Run a sync pydal job on the shared DAL executor (same pattern as `PoolStore`)."""
    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, job)


async def get_tenant_license_key(async_dal: Any, dal: Any, tenant_id: int) -> str | None:
    """Return the tenant's decrypted license key, or None when unset/unreadable.

    A corrupt/undecryptable row or a seal version < 1 logs the exception type only and yields
    None (the pool provider then raises ``PoolUnavailable('no_tenant_license')``): a loud failure
    for the pool, never a default key. Database errors propagate (transient for the provider).
    """
    bind_tenant_license_key_tables(dal)

    def _job() -> tuple[str, int] | None:
        try:
            t = dal.tenant_license_keys
            row = dal(t.tenant_id == tenant_id).select().first()
            if row is None:
                return None
            return str(row.license_key_sealed), int(row.seal_version)
        finally:
            dal.rollback()

    found = await _run(async_dal, _job)
    if found is None:
        return None
    sealed, seal_version = found
    if seal_version < _MIN_SEAL_VERSION:
        logger.error("tenant_license.key_unavailable type=InvalidSealVersion")
        return None
    try:
        return vault.open_secret(sealed, aad=_aad(tenant_id), seal_version=seal_version)
    except PlatformCredentialCryptoError as exc:
        logger.error("tenant_license.key_unavailable type=%s", type(exc).__name__)
        return None


async def get_license_status(async_dal: Any, dal: Any, tenant_id: int) -> LicenseStatus:
    """Return `{configured, fingerprint, set_at}` only (no ciphertext is even selected out)."""
    bind_tenant_license_key_tables(dal)

    def _job() -> LicenseStatus:
        try:
            t = dal.tenant_license_keys
            row = (
                dal(t.tenant_id == tenant_id)
                .select(t.license_key_fingerprint, t.license_key_set_at)
                .first()
            )
            if row is None:
                return LicenseStatus(False, None, None)
            return LicenseStatus(
                True, str(row.license_key_fingerprint), _aware(row.license_key_set_at)
            )
        finally:
            dal.rollback()

    return await _run(async_dal, _job)  # type: ignore[no-any-return]


def _event(
    action: AuditAction, tenant_id: int, actor: int | None, details: dict[str, Any]
) -> AuditEvent:
    """Build a license audit event (details are fingerprint/limit only, never a key)."""
    return AuditEvent(
        category=AuditCategory.LICENSE,
        action=action.value,
        outcome=AuditOutcome.SUCCESS,
        actor_kind=ActorKind.USER if actor is not None else ActorKind.SYSTEM,
        actor_user_id=actor,
        tenant_id=tenant_id,
        target_type="tenant_license",
        target_id=str(tenant_id),
        details=details,
    )


def _evict_cached_pools(dal: Any, tenant_id: int) -> None:
    """Drop the tenant's license_server-cached pool rows in the caller's transaction.

    A cached pool must never outlive the key that produced it. ``operator_set`` rows are kept.
    """
    bind_community_tier_tables(dal)
    t = dal.tenant_license_pools
    dal(
        (t.tenant_id == tenant_id)
        & (t.axis_code == _AXIS_TEAM)
        & (t.source_code == _POOL_SOURCE_LICENSE_SERVER)
    ).delete()


async def _audit(audit: Any | None, event: AuditEvent) -> None:
    """Audit after the commit: one retry, then a LOUD failure (ERROR + metric, re-raised)."""
    if audit is None:
        return
    try:
        await audit.record(event)
        return
    except Exception as first:  # noqa: BLE001 - retried once, then re-raised
        logger.warning("tenant_license.audit_retry type=%s", type(first).__name__)
    try:
        await audit.record(event)
    except Exception as exc:
        telemetry.audit_write_failed.add(1, {"area": "tenant_license"})
        logger.error(
            "tenant_license.audit_write_failed action=%s type=%s", event.action, type(exc).__name__
        )
        raise


async def set_tenant_license_key(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    license_key: object,
    actor: int | None,
    audit: Any | None = None,
) -> LicenseStatus:
    """Validate, seal and upsert the tenant's key; audit `license.tenant_key_set`."""
    key = validate_license_key(license_key)
    bind_tenant_license_key_tables(dal)
    try:
        sealed = vault.seal_secret(key, aad=_aad(tenant_id))
    except PlatformCredentialCryptoError:
        logger.error("tenant_license.seal_failed")
        raise ApiError("License key storage is unavailable", 503, "SEAL_UNAVAILABLE") from None
    fingerprint = fingerprint_of(key)
    now = datetime.now(UTC)

    def _job() -> None:
        try:
            t = dal.tenant_license_keys
            fields = {
                "license_key_sealed": sealed,
                "seal_version": vault.SEAL_AAD,
                "license_key_fingerprint": fingerprint,
                "license_key_set_at": now,
                "license_key_set_by": actor,
            }
            if dal(t.tenant_id == tenant_id).update(**fields) == 0:
                t.insert(tenant_id=tenant_id, **fields)
            _evict_cached_pools(dal, tenant_id)
            dal.commit()
        except Exception:
            dal.rollback()
            raise

    await _run(async_dal, _job)
    await _audit(
        audit,
        _event(AuditAction.LICENSE_TENANT_KEY_SET, tenant_id, actor, {"fingerprint": fingerprint}),
    )
    return LicenseStatus(True, fingerprint, now)


async def clear_tenant_license_key(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    actor: int | None,
    audit: Any | None = None,
) -> bool:
    """Delete the tenant's key; audit `license.tenant_key_cleared`. Returns whether one existed."""
    bind_tenant_license_key_tables(dal)

    def _job() -> int:
        try:
            deleted = int(dal(dal.tenant_license_keys.tenant_id == tenant_id).delete())
            _evict_cached_pools(dal, tenant_id)
            dal.commit()
            return deleted
        except Exception:
            dal.rollback()
            raise

    existed = bool(await _run(async_dal, _job))
    if existed:
        await _audit(audit, _event(AuditAction.LICENSE_TENANT_KEY_CLEARED, tenant_id, actor, {}))
    return existed


#: Largest accepted pool limit (the DB column is a 32-bit integer).
MAX_POOL_LIMIT: Final[int] = 2_147_483_647


def require_valid_limit(value: object) -> int | None:
    """Validate an operator pool limit: None clears, else -1..int32 max (bools rejected)."""
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < -1
        or value > MAX_POOL_LIMIT
    ):
        raise bad_request(
            f"limit must be null, -1 (unlimited) or an integer between 0 and {MAX_POOL_LIMIT}"
        )
    return value
