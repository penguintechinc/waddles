"""Per-tenant envelope encryption with an optional customer-managed KEK (BYOK).

Implements `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-
design.md` Sec2 (field AEAD), Sec4 (key lifecycle) and Sec6 (Enterprise
BYOK):

```
data  --AES-256-GCM(data subkey, AAD)-->  ciphertext
data subkey = HKDF(tenant DEK)
tenant DEK  --wrapped by-->  KEK = platform baseline (default)
                                 | customer KMS key (Enterprise, on top)
```

Policy decisions (each is a deliberate reading of the design, tested):

- **Baseline is the default; BYOK is additive.** No config (or a config that is
  not ``active``) means the platform KEK. A tenant is never moved onto, or
  silently *back* onto, a different KEK as a side effect: new key material
  goes to the KEK the tenant explicitly activated, and a pending/revoked
  BYOK state fails closed instead of downgrading.
- **Entitlement gates new BYOK key material only.** Configure / activate /
  wrap-a-new-DEK under the customer key require ``compliance.external_kms``.
  Unwrapping an existing DEK never does, and the exit back to the baseline is
  never gated -- a lapsed licence must not strand encrypted data.
- **KMS access denied = revocation** (design Sec6): the tenant's cached keys
  are evicted at once, both encrypt and decrypt fail closed, ops are alerted
  (ERROR log + metric), and the config is flagged ``revoked``. No fallback KEK.
- **KMS unreachable = transient**: while the cache is fresh (TTL, default 10
  min) nothing changes. Past the TTL, *new* data is refused (fail closed -- we
  will not mint ciphertext under a key we cannot currently authorize), but
  *existing* data stays readable from the stale cache for a bounded grace
  window (default +15 min), then fails closed too. Nothing is lost: the
  wrapped DEK stays in the key store and access resumes when the KMS returns.
- **One tenant's KMS never affects another's**: per-tenant lock, failure
  backoff, and cache; KMS calls run on a bounded pool under a hard timeout.
- **Never persist an un-unwrappable DEK**: every wrap (create, rotate,
  re-wrap) is verified by unwrapping the result before it is stored, and
  re-wraps are compare-and-swap, so a failure leaves the old, working row.

The cache holds only the HKDF data subkey (never the raw DEK). Nothing in
this module logs key bytes, wrapped bytes, plaintext, ciphertext or
credentials -- only tenant ids, versions, and error codes.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from services.envelope import metrics
from services.envelope.crypto import (
    KEY_LENGTH,
    derive_data_subkey,
    field_aad,
    open_sealed,
    seal,
    validate_tenant_id,
    wrap_context,
)
from services.envelope.errors import (
    EnvelopeError,
    EnvelopeInputError,
    EnvelopeIntegrityError,
    ExternalKmsNotEntitledError,
    KmsAccessDeniedError,
    KmsCredentialsError,
    KmsError,
    KmsRejectedError,
    KmsUnavailableError,
    TenantKeyNotFoundError,
    TenantKeyUnavailableError,
    UnavailableReason,
)
from services.envelope.gate import ExternalKmsEntitlement, current_request_host
from services.envelope.kms_adapter import KmsAdapter, KmsProviderRegistry
from services.envelope.models import (
    CONFIG_ACTIVE,
    CONFIG_PENDING,
    CONFIG_REVOKED,
    KEK_KIND_CUSTOMER,
    KEK_KIND_PLATFORM,
    KEY_DESTROYED,
    DekRecord,
    EncryptedField,
    RewrapReport,
    TenantKmsConfig,
)
from services.envelope.repository import (
    EnvelopeKeyRepository,
    KeyConflictError,
    KmsConfigRepository,
)

logger = logging.getLogger(__name__)

#: Resolves a tenant id to its slug (the entitlement system keys on slugs).
SlugResolver = Callable[[int], Awaitable[str | None]]


@dataclass(slots=True, frozen=True)
class EnvelopeSettings:
    """Tunables for caching, failure handling and the nonce-usage cap (all bounded)."""

    #: Design Sec4: unwrapped keys are cached for 10 minutes.
    dek_cache_ttl_s: float = 600.0
    #: Extra window past the TTL during which *reads* may use a stale key if the
    #: KMS is unreachable. Writes never get a grace window.
    stale_grace_s: float = 900.0
    #: After an access-denied, refuse to call the KMS again for this long.
    denied_backoff_s: float = 30.0
    #: After a transient failure, fail fast for this long instead of piling on.
    transient_backoff_s: float = 5.0
    #: Persist the in-process encryption counter every N operations.
    usage_flush_batch: int = 1024
    #: Design Sec2: auto-rotate a DEK version at 2^30 encryptions (NIST SP
    #: 800-38D random-nonce margin is 2^32).
    usage_cap: int = 2**30

    def __post_init__(self) -> None:
        """Reject out-of-range values so a bad env var cannot disable a safety property."""
        if not 0 < self.dek_cache_ttl_s <= 3600:
            raise ValueError("dek_cache_ttl_s must be in (0, 3600]")
        if not 0 <= self.stale_grace_s <= 3600:
            raise ValueError("stale_grace_s must be in [0, 3600]")
        if self.denied_backoff_s < 0 or self.transient_backoff_s < 0:
            raise ValueError("backoff values must be >= 0")
        if self.usage_flush_batch < 1 or self.usage_cap < self.usage_flush_batch:
            raise ValueError("usage_cap must be >= usage_flush_batch >= 1")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EnvelopeSettings:
        """Read ``ENVELOPE_*`` overrides (all optional); invalid values raise ``ValueError``."""
        env = os.environ if environ is None else environ
        defaults = cls()
        return cls(
            dek_cache_ttl_s=float(env.get("ENVELOPE_DEK_CACHE_TTL_S", defaults.dek_cache_ttl_s)),
            stale_grace_s=float(env.get("ENVELOPE_STALE_GRACE_S", defaults.stale_grace_s)),
            denied_backoff_s=float(env.get("ENVELOPE_DENIED_BACKOFF_S", defaults.denied_backoff_s)),
            transient_backoff_s=float(
                env.get("ENVELOPE_TRANSIENT_BACKOFF_S", defaults.transient_backoff_s)
            ),
            usage_flush_batch=int(
                env.get("ENVELOPE_USAGE_FLUSH_BATCH", defaults.usage_flush_batch)
            ),
            usage_cap=int(env.get("ENVELOPE_USAGE_CAP", defaults.usage_cap)),
        )


@dataclass(slots=True)
class _CachedKey:
    """One cached data subkey and when it was last verified against the KEK."""

    subkey: bytes = field(repr=False)
    fetched_at: float


@dataclass(slots=True)
class _TenantState:
    """Per-tenant in-process state: lock, key cache, failure backoff, usage counter."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    keys: dict[int, _CachedKey] = field(default_factory=dict)
    adapters: dict[tuple[str, ...], KmsAdapter] = field(default_factory=dict)
    active_version: int | None = None
    active_checked_at: float = 0.0
    blocked_until: float = 0.0
    blocked_reason: UnavailableReason | None = None
    pending_usage: int = 0
    slug: str | None = None
    flagged_revoked: bool = False


@dataclass(slots=True, frozen=True)
class TenantKmsStatus:
    """A tenant's KMS config plus a no-secrets summary of its DEK versions."""

    config: TenantKmsConfig | None
    keys: tuple[DekRecord, ...]
    supported_providers: tuple[str, ...]


class TenantEnvelopeService:
    """Encrypt/decrypt tenant data under a per-tenant DEK, wrapped by a platform or customer KEK."""

    def __init__(
        self,
        *,
        keys: EnvelopeKeyRepository,
        configs: KmsConfigRepository,
        registry: KmsProviderRegistry,
        platform_kek: Callable[[], KmsAdapter],
        gate: ExternalKmsEntitlement,
        settings: EnvelopeSettings | None = None,
        slug_resolver: SlugResolver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the service; every collaborator is injectable (tests use in-memory fakes)."""
        self._keys = keys
        self._configs = configs
        self._registry = registry
        self._platform = platform_kek
        self._gate = gate
        self._settings = settings or EnvelopeSettings()
        self._slug_resolver = slug_resolver
        self._clock = clock
        self._states: dict[int, _TenantState] = {}

    def _state(self, tenant_id: int) -> _TenantState:
        """Return (creating on demand) the in-process state for `tenant_id`."""
        state = self._states.get(validate_tenant_id(tenant_id))
        if state is None:
            state = _TenantState()
            self._states[tenant_id] = state
        return state

    def invalidate(self, tenant_id: int) -> None:
        """Drop a tenant's cached keys and failure backoff (used after config/rotation changes)."""
        state = self._states.get(tenant_id)
        if state is None:
            return
        state.keys.clear()
        state.adapters.clear()
        state.active_version = None
        state.blocked_until = 0.0
        state.blocked_reason = None
        state.pending_usage = 0

    def _blocked_error(self, state: _TenantState) -> TenantKeyUnavailableError:
        reason: UnavailableReason = state.blocked_reason or "kms_blocked"
        return TenantKeyUnavailableError(
            f"tenant encryption key unavailable ({reason}); retry later", reason=reason
        )

    def _start_backoff(
        self, state: _TenantState, reason: UnavailableReason, seconds: float
    ) -> None:
        state.blocked_until = self._clock() + seconds
        state.blocked_reason = reason

    async def _tenant_slug(self, tenant_id: int, tenant_slug: str | None) -> str | None:
        """Resolve (and remember) the tenant slug the entitlement system keys on."""
        state = self._state(tenant_id)
        if tenant_slug:
            state.slug = tenant_slug
            return tenant_slug
        if state.slug is None and self._slug_resolver is not None:
            state.slug = await self._slug_resolver(tenant_id)
        return state.slug

    async def _require_entitlement(self, tenant_id: int, tenant_slug: str | None) -> None:
        """Raise unless the tenant is positively entitled to external KMS (fail closed)."""
        slug = await self._tenant_slug(tenant_id, tenant_slug)
        if not slug or not await self._gate.is_entitled(slug, request_host=current_request_host()):
            logger.info(
                "envelope.kms.not_entitled",
                extra={"tenant_id": tenant_id, "feature": "compliance.external_kms"},
            )
            metrics.record_operation("entitlement", "denied")
            raise ExternalKmsNotEntitledError(
                "external KMS requires the Enterprise compliance.external_kms entitlement"
            )

    async def _resolve_target(self, tenant_id: int, tenant_slug: str | None) -> KmsAdapter:
        """Pick the KEK that NEW key material must be wrapped under, or fail closed.

        No config -> platform baseline. ``active`` config -> customer KMS (and
        the entitlement is re-checked). ``pending``/``revoked`` -> platform only
        if the tenant has no customer-wrapped DEKs at all; otherwise raise
        rather than silently downgrade the isolation the customer chose.
        """
        config = await self._configs.get(tenant_id)
        if config is None:
            return self._platform()
        if config.status == CONFIG_ACTIVE:
            await self._require_entitlement(tenant_id, tenant_slug)
            return self._registry.build(config)
        if config.status == CONFIG_REVOKED:
            raise TenantKeyUnavailableError(
                "the customer KMS key was revoked; re-activate or disable external KMS",
                reason="kms_access_denied",
            )
        rows = await self._keys.list_keys(tenant_id)
        if any(row.kek_kind == KEK_KIND_CUSTOMER for row in rows):
            raise TenantKeyUnavailableError(
                "external KMS reconfiguration is pending; activate it before new key material",
                reason="kms_pending",
            )
        return self._platform()

    async def _adapter_for_record(self, tenant_id: int, record: DekRecord) -> KmsAdapter:
        """Return the adapter that wrapped `record` (platform, or the customer key it names)."""
        if record.kek_kind == KEK_KIND_PLATFORM:
            return self._platform()
        if record.kek_kind != KEK_KIND_CUSTOMER:
            raise EnvelopeError(f"unknown kek_kind {record.kek_kind!r}")
        config = await self._configs.get(tenant_id)
        if config is None:
            raise TenantKeyUnavailableError(
                "external KMS configuration is missing for a customer-wrapped key",
                reason="kms_config_missing",
            )
        state = self._state(tenant_id)
        cache_key = (
            config.provider,
            record.kek_ref,
            config.principal or "",
            config.region or "",
            config.external_id,
        )
        adapter = state.adapters.get(cache_key)
        if adapter is None:
            adapter = self._registry.build(config, key_ref=record.kek_ref)
            state.adapters[cache_key] = adapter
        return adapter

    async def _unwrap_to_subkey(self, tenant_id: int, record: DekRecord) -> bytes:
        """Unwrap `record` and derive its data subkey (the raw DEK never leaves this frame)."""
        if record.wrapped_dek is None or record.status == KEY_DESTROYED:
            raise TenantKeyNotFoundError("data key has been destroyed")
        adapter = await self._adapter_for_record(tenant_id, record)
        dek = await adapter.unwrap(record.wrapped_dek, context=wrap_context(tenant_id))
        return derive_data_subkey(dek, tenant_id)

    async def _load_subkey(
        self,
        state: _TenantState,
        tenant_id: int,
        record: DekRecord,
        *,
        stale: _CachedKey | None = None,
    ) -> bytes:
        """Unwrap + cache a key, translating KMS failures into the design's failure policy.

        `stale` (reads only) is a previously cached entry that may be served if
        the KMS is merely unreachable and the grace window has not elapsed.
        """
        try:
            subkey = await self._unwrap_to_subkey(tenant_id, record)
        except KmsAccessDeniedError as exc:
            await self._on_revoked(state, tenant_id, record, exc)
            raise TenantKeyUnavailableError(
                "tenant encryption key unavailable: access denied by the key provider",
                reason="kms_access_denied",
            ) from exc
        except KmsUnavailableError as exc:
            platform_credentials = isinstance(exc, KmsCredentialsError)
            reason: UnavailableReason = (
                "kms_platform_credentials" if platform_credentials else "kms_unavailable"
            )
            self._start_backoff(state, reason, self._settings.transient_backoff_s)
            metrics.record_kms_failure(
                record.kek_kind,
                "platform_credentials" if platform_credentials else "unavailable",
                tenant_id,
            )
            age = self._clock() - stale.fetched_at if stale is not None else None
            if (
                stale is not None
                and age is not None
                and age < self._settings.dek_cache_ttl_s + self._settings.stale_grace_s
            ):
                logger.warning(
                    "envelope.kms.serving_stale_key",
                    extra={"tenant_id": tenant_id, "dek_version": record.dek_version},
                )
                metrics.record_cache("stale")
                return stale.subkey
            logger.error(
                "envelope.kms.unavailable",
                extra={
                    "tenant_id": tenant_id,
                    "code": getattr(exc, "code", None),
                    "reason": reason,
                    "alert": "kms-platform-credentials" if platform_credentials else None,
                },
            )
            raise TenantKeyUnavailableError(
                "tenant encryption key unavailable: key provider unreachable",
                reason=reason,
            ) from exc
        except (KmsError, EnvelopeIntegrityError) as exc:
            state.keys.pop(record.dek_version, None)
            self._start_backoff(state, "kms_rejected", self._settings.transient_backoff_s)
            metrics.record_kms_failure(record.kek_kind, "rejected", tenant_id)
            logger.error(
                "envelope.kms.rejected",
                extra={"tenant_id": tenant_id, "error": type(exc).__name__},
                exc_info=True,
            )
            raise TenantKeyUnavailableError(
                "tenant encryption key unavailable: key provider rejected the key",
                reason="kms_rejected",
            ) from exc
        state.keys[record.dek_version] = _CachedKey(subkey, self._clock())
        state.blocked_until = 0.0
        state.blocked_reason = None
        await self._heal_revoked_flag(state, tenant_id, record)
        return subkey

    async def _on_revoked(
        self, state: _TenantState, tenant_id: int, record: DekRecord, exc: KmsAccessDeniedError
    ) -> None:
        """Handle an access-denied: evict, back off, alert, and flag the config (best effort)."""
        state.keys.clear()
        state.active_version = None
        self._start_backoff(state, "kms_access_denied", self._settings.denied_backoff_s)
        metrics.record_kms_failure(record.kek_kind, "access_denied", tenant_id)
        logger.error(
            "envelope.kms.access_denied",
            extra={
                "tenant_id": tenant_id,
                "dek_version": record.dek_version,
                "code": exc.code,
                "alert": "tenant-kms-revoked",
            },
        )
        if record.kek_kind != KEK_KIND_CUSTOMER:
            return
        try:
            await self._configs.set_status(tenant_id, CONFIG_REVOKED, error_code=exc.code)
            state.flagged_revoked = True
        except Exception:
            logger.exception("envelope.kms.revoked_flag_failed", extra={"tenant_id": tenant_id})

    async def _heal_revoked_flag(
        self, state: _TenantState, tenant_id: int, record: DekRecord
    ) -> None:
        """If this process flagged the config revoked and access is back, restore ``active``."""
        if not state.flagged_revoked or record.kek_kind != KEK_KIND_CUSTOMER:
            return
        try:
            await self._configs.set_status(tenant_id, CONFIG_ACTIVE, verified=True)
            state.flagged_revoked = False
            logger.info("envelope.kms.access_restored", extra={"tenant_id": tenant_id})
        except Exception:
            logger.exception("envelope.kms.restore_flag_failed", extra={"tenant_id": tenant_id})

    async def _create_key(
        self,
        state: _TenantState,
        tenant_id: int,
        adapter: KmsAdapter,
        *,
        expected_version: int | None,
    ) -> DekRecord:
        """Mint a DEK, wrap it, verify the wrap round-trips, persist it as the active version."""
        dek = secrets.token_bytes(KEY_LENGTH)
        context = wrap_context(tenant_id)
        wrapped = await adapter.wrap(dek, context=context)
        if not hmac.compare_digest(await adapter.unwrap(wrapped, context=context), dek):
            raise KmsRejectedError("freshly wrapped DEK failed its unwrap check")
        if expected_version is None:
            # A KeyConflictError here means another replica created version 1 first;
            # the caller re-reads the active row rather than rotating.
            record = await self._keys.insert_active(
                tenant_id, wrapped, adapter.kek_kind, adapter.key_ref
            )
        else:
            record = await self._keys.rotate_active(
                tenant_id,
                expected_version=expected_version,
                wrapped_dek=wrapped,
                kek_kind=adapter.kek_kind,
                kek_ref=adapter.key_ref,
            )
        now = self._clock()
        state.keys[record.dek_version] = _CachedKey(derive_data_subkey(dek, tenant_id), now)
        state.active_version = record.dek_version
        state.active_checked_at = now
        state.pending_usage = 0
        return record

    async def _provision_first_key(
        self, state: _TenantState, tenant_id: int, tenant_slug: str | None
    ) -> DekRecord:
        """Create version 1 for a tenant that has none (lock must be held by the caller)."""
        adapter = await self._resolve_target(tenant_id, tenant_slug)
        try:
            record = await self._create_key(state, tenant_id, adapter, expected_version=None)
        except KeyConflictError:
            existing = await self._keys.get_active(tenant_id)
            if existing is None:
                raise
            return existing
        logger.info(
            "envelope.key.created",
            extra={
                "tenant_id": tenant_id,
                "dek_version": record.dek_version,
                "kek": record.kek_kind,
            },
        )
        metrics.record_operation("create_key", "ok")
        return record

    async def _active_subkey(self, tenant_id: int) -> tuple[int, bytes]:
        """Return ``(version, subkey)`` for encrypting NEW data -- needs a fresh, verified key."""
        state = self._state(tenant_id)
        ttl = self._settings.dek_cache_ttl_s
        now = self._clock()
        version = state.active_version
        if version is not None and now - state.active_checked_at < ttl:
            entry = state.keys.get(version)
            if entry is not None and now - entry.fetched_at < ttl:
                metrics.record_cache("hit")
                return version, entry.subkey
        metrics.record_cache("miss")
        async with state.lock:
            now = self._clock()
            version = state.active_version
            if version is not None and now - state.active_checked_at < ttl:
                entry = state.keys.get(version)
                if entry is not None and now - entry.fetched_at < ttl:
                    return version, entry.subkey
            if state.blocked_until > now:
                raise self._blocked_error(state)
            record = await self._keys.get_active(tenant_id)
            if record is None:
                record = await self._provision_first_key(state, tenant_id, None)
            entry = state.keys.get(record.dek_version)
            if entry is not None and now - entry.fetched_at < ttl:
                state.active_version = record.dek_version
                state.active_checked_at = now
                return record.dek_version, entry.subkey
            subkey = await self._load_subkey(state, tenant_id, record)  # no stale for writes
            state.active_version = record.dek_version
            state.active_checked_at = self._clock()
            return record.dek_version, subkey

    async def _subkey_for_version(self, tenant_id: int, version: int) -> bytes:
        """Return the subkey for decrypting existing data (stale allowed within the grace)."""
        state = self._state(tenant_id)
        ttl = self._settings.dek_cache_ttl_s
        entry = state.keys.get(version)
        if entry is not None and self._clock() - entry.fetched_at < ttl:
            metrics.record_cache("hit")
            return entry.subkey
        metrics.record_cache("miss")
        async with state.lock:
            now = self._clock()
            entry = state.keys.get(version)
            if entry is not None and now - entry.fetched_at < ttl:
                return entry.subkey
            if state.blocked_until > now:
                grace_ok = (
                    entry is not None
                    and state.blocked_reason in ("kms_unavailable", "kms_platform_credentials")
                    and now - entry.fetched_at < ttl + self._settings.stale_grace_s
                )
                if grace_ok and entry is not None:
                    metrics.record_cache("stale")
                    return entry.subkey
                raise self._blocked_error(state)
            record = await self._keys.get_version(tenant_id, version)
            if record is None or record.wrapped_dek is None or record.status == KEY_DESTROYED:
                raise TenantKeyNotFoundError(f"no data key version {version} for this tenant")
            return await self._load_subkey(state, tenant_id, record, stale=entry)

    async def encrypt(
        self,
        tenant_id: int,
        plaintext: bytes,
        *,
        table: str,
        column: str,
        row_uuid: str | uuid.UUID,
    ) -> EncryptedField:
        """Encrypt `plaintext` for one (tenant, table, column, row) slot under the active DEK.

        Fails closed (:class:`TenantKeyUnavailableError`) rather than ever
        writing ciphertext under an unverifiable key, and never falls back to
        another KEK. A tenant with no key gets version 1 provisioned on first
        use (on the platform KEK unless BYOK is active).
        """
        validate_tenant_id(tenant_id)
        version, subkey = await self._active_subkey(tenant_id)
        aad = field_aad(tenant_id, table, column, row_uuid, version)
        iv, ciphertext = seal(subkey, plaintext, aad)
        metrics.record_operation("encrypt", "ok")
        await self._count_usage(tenant_id, version)
        return EncryptedField(dek_version=version, iv=iv, ciphertext=ciphertext)

    async def decrypt(
        self,
        tenant_id: int,
        encrypted: EncryptedField,
        *,
        table: str,
        column: str,
        row_uuid: str | uuid.UUID,
    ) -> bytes:
        """Decrypt a value for its exact (tenant, table, column, row) slot.

        Raises:
            EnvelopeIntegrityError: tampering, or the value was moved from
                another tenant/table/column/row.
            TenantKeyUnavailableError: the KEK is revoked/unreachable (and no
                stale key is within its grace window).
        """
        validate_tenant_id(tenant_id)
        aad = field_aad(tenant_id, table, column, row_uuid, encrypted.dek_version)
        subkey = await self._subkey_for_version(tenant_id, encrypted.dek_version)
        try:
            plaintext = open_sealed(subkey, encrypted.iv, encrypted.ciphertext, aad)
        except EnvelopeIntegrityError:
            metrics.record_operation("decrypt", "integrity_failure")
            raise
        metrics.record_operation("decrypt", "ok")
        return plaintext

    async def encrypt_text(
        self, tenant_id: int, text: str, *, table: str, column: str, row_uuid: str | uuid.UUID
    ) -> str:
        """Encrypt a string to the single-column wire form (``wenv1.<version>.<blob>``)."""
        encrypted = await self.encrypt(
            tenant_id, text.encode("utf-8"), table=table, column=column, row_uuid=row_uuid
        )
        return encrypted.to_wire()

    async def decrypt_text(
        self, tenant_id: int, wire: str, *, table: str, column: str, row_uuid: str | uuid.UUID
    ) -> str:
        """Decrypt :meth:`encrypt_text` output back to a string."""
        plaintext = await self.decrypt(
            tenant_id,
            EncryptedField.from_wire(wire),
            table=table,
            column=column,
            row_uuid=row_uuid,
        )
        return plaintext.decode("utf-8")

    async def _count_usage(self, tenant_id: int, version: int) -> None:
        """Count one encryption; persist in batches and auto-rotate at the nonce-safety cap."""
        state = self._state(tenant_id)
        state.pending_usage += 1
        if state.pending_usage < self._settings.usage_flush_batch:
            return
        batch, state.pending_usage = state.pending_usage, 0
        try:
            total = await self._keys.add_usage(tenant_id, version, batch)
        except Exception:
            state.pending_usage += batch
            logger.exception("envelope.usage.flush_failed", extra={"tenant_id": tenant_id})
            return
        if total >= self._settings.usage_cap:
            logger.warning(
                "envelope.key.usage_cap_reached",
                extra={"tenant_id": tenant_id, "dek_version": version, "usage": total},
            )
            try:
                await self.rotate_dek(tenant_id, reason="usage_cap")
            except Exception:
                logger.exception("envelope.key.auto_rotate_failed", extra={"tenant_id": tenant_id})

    async def ensure_tenant_key(
        self, tenant_id: int, *, tenant_slug: str | None = None
    ) -> DekRecord:
        """Return the tenant's active DEK row, creating version 1 if there is none."""
        state = self._state(tenant_id)
        async with state.lock:
            record = await self._keys.get_active(tenant_id)
            if record is None:
                record = await self._provision_first_key(state, tenant_id, tenant_slug)
            return record

    async def rotate_dek(
        self, tenant_id: int, *, tenant_slug: str | None = None, reason: str = "manual"
    ) -> DekRecord:
        """Mint a new DEK version, retiring (never deleting) the previous active one.

        Old ciphertext stays decryptable: the retired row keeps its wrapped DEK.
        New writes use the new version immediately on this replica; other
        replicas converge within the cache TTL.
        """
        state = self._state(tenant_id)
        async with state.lock:
            adapter = await self._resolve_target(tenant_id, tenant_slug)
            current = await self._keys.get_active(tenant_id)
            record = await self._create_key(
                state,
                tenant_id,
                adapter,
                expected_version=current.dek_version if current is not None else None,
            )
        logger.info(
            "envelope.key.rotated",
            extra={
                "tenant_id": tenant_id,
                "dek_version": record.dek_version,
                "kek": record.kek_kind,
                "reason": reason,
            },
        )
        metrics.record_operation("rotate", "ok")
        return record

    async def _rewrap_rows(self, tenant_id: int, target: KmsAdapter) -> RewrapReport:
        """Re-wrap every DEK version onto `target`; per-row, verified, CAS, resumable.

        The DEKs themselves never change, so no data is re-encrypted. A row
        that fails stays on its previous KEK (still readable); re-running
        picks up exactly the rows not yet on `target`.
        """
        rows = await self._keys.list_keys(tenant_id)
        context = wrap_context(tenant_id)
        rewrapped = current = 0
        failed: list[int] = []
        for row in rows:
            if row.kek_kind == target.kek_kind and row.kek_ref == target.key_ref:
                current += 1
                metrics.record_rewrap_row("current")
                continue
            try:
                if row.wrapped_dek is None:
                    raise TenantKeyNotFoundError("data key has been destroyed")
                source = await self._adapter_for_record(tenant_id, row)
                dek = await source.unwrap(row.wrapped_dek, context=context)
                new_wrapped = await target.wrap(dek, context=context)
                if not hmac.compare_digest(await target.unwrap(new_wrapped, context=context), dek):
                    raise KmsRejectedError("re-wrapped DEK failed its unwrap check")
                swapped = await self._keys.replace_wrapped(
                    row, new_wrapped=new_wrapped, kek_kind=target.kek_kind, kek_ref=target.key_ref
                )
                if not swapped:
                    raise KeyConflictError("key row changed during re-wrap")
            except EnvelopeError as exc:
                failed.append(row.dek_version)
                metrics.record_rewrap_row("failed")
                logger.error(
                    "envelope.rewrap.row_failed",
                    extra={
                        "tenant_id": tenant_id,
                        "dek_version": row.dek_version,
                        "error": type(exc).__name__,
                    },
                )
                continue
            rewrapped += 1
            metrics.record_rewrap_row("rewrapped")
        report = RewrapReport(
            tenant_id=tenant_id,
            target_kind=target.kek_kind,
            target_ref=target.key_ref,
            total=len(rows),
            rewrapped=rewrapped,
            already_current=current,
            failed_versions=tuple(failed),
        )
        logger.info(
            "envelope.rewrap.done",
            extra={
                "tenant_id": tenant_id,
                "total": report.total,
                "rewrapped": rewrapped,
                "current": current,
                "failed": len(failed),
                "kek": target.kek_kind,
            },
        )
        metrics.record_operation("rewrap", "ok" if report.ok else "partial")
        return report

    async def rewrap_tenant(
        self, tenant_id: int, *, tenant_slug: str | None = None
    ) -> RewrapReport:
        """Re-wrap every DEK onto the tenant's current target KEK (KEK-rotation / resume path)."""
        validate_tenant_id(tenant_id)
        state = self._state(tenant_id)
        async with state.lock:
            target = await self._resolve_target(tenant_id, tenant_slug)
            return await self._rewrap_rows(tenant_id, target)

    async def get_status(self, tenant_id: int) -> TenantKmsStatus:
        """Return the tenant's config and a no-secrets summary of its DEK versions."""
        validate_tenant_id(tenant_id)
        return TenantKmsStatus(
            config=await self._configs.get(tenant_id),
            keys=tuple(await self._keys.list_keys(tenant_id)),
            supported_providers=self._registry.supported(),
        )

    async def configure_external_kms(
        self,
        tenant_id: int,
        *,
        tenant_slug: str,
        provider: str,
        key_ref: str,
        region: str | None,
        principal: str | None,
    ) -> TenantKmsConfig:
        """Record the tenant's customer-key config as ``pending`` (entitlement + validation first).

        Moves no key material and changes no KEK: the tenant stays on its
        current KEK until :meth:`activate_external_kms` succeeds. The
        ExternalId (server-generated, stable across edits) is returned so the
        customer can pin it in their role's trust policy (AWS) or write it as a
        label/tag on their key (GCP/Azure) -- the proof-of-control token.
        """
        validate_tenant_id(tenant_id)
        await self._require_entitlement(tenant_id, tenant_slug)
        canonical = self._registry.validate(
            provider, key_ref=key_ref, region=region, principal=principal
        )
        existing = await self._configs.get(tenant_id)
        if existing is not None and existing.provider != provider:
            rows = await self._keys.list_keys(tenant_id)
            if any(row.kek_kind == KEK_KIND_CUSTOMER for row in rows):
                raise EnvelopeInputError(
                    "provider cannot change while DEKs are wrapped by the current provider; "
                    "disable external KMS first"
                )
        config = await self._configs.upsert(
            tenant_id,
            provider=provider,
            key_ref=canonical.key_ref,
            region=canonical.region,
            principal=canonical.principal,
            # Lowercase hex (192 bits): satisfies AWS ExternalId, GCP label-value
            # (<=63 chars, [a-z0-9_-]) and Azure tag-value rules with one token.
            new_external_id=secrets.token_hex(24),
        )
        self.invalidate(tenant_id)
        logger.info(
            "envelope.kms.configured",
            extra={"tenant_id": tenant_id, "provider": provider, "status": config.status},
        )
        metrics.record_operation("configure", "ok")
        return config

    async def activate_external_kms(
        self, tenant_id: int, *, tenant_slug: str
    ) -> tuple[TenantKmsConfig, RewrapReport]:
        """Verify the customer key, move every DEK onto it, and flip the config to ``active``.

        The config only becomes ``active`` when *all* DEK versions sit under
        the customer key; a partial result leaves it ``pending`` (rows remain
        readable on their previous KEK) and is safe to re-run.

        Raises:
            ExternalKmsNotEntitledError: not entitled.
            EnvelopeInputError: no config to activate.
            KmsError: the preflight failed (access denied, unreachable, bad key).
        """
        validate_tenant_id(tenant_id)
        await self._require_entitlement(tenant_id, tenant_slug)
        config = await self._configs.get(tenant_id)
        if config is None:
            raise EnvelopeInputError("no external KMS configuration to activate")
        adapter = self._registry.build(config)
        try:
            await adapter.verify()
        except KmsError as exc:
            await self._configs.set_status(
                tenant_id, config.status, error_code=exc.code or type(exc).__name__
            )
            metrics.record_operation("activate", "verify_failed")
            raise
        state = self._state(tenant_id)
        async with state.lock:
            existing = await self._keys.list_keys(tenant_id)
            if not existing:
                await self._create_key(state, tenant_id, adapter, expected_version=None)
                report = RewrapReport(
                    tenant_id=tenant_id,
                    target_kind=adapter.kek_kind,
                    target_ref=adapter.key_ref,
                    total=1,
                    rewrapped=0,
                    already_current=1,
                )
            else:
                report = await self._rewrap_rows(tenant_id, adapter)
        status = CONFIG_ACTIVE if report.ok else CONFIG_PENDING
        await self._configs.set_status(tenant_id, status, verified=True)
        self.invalidate(tenant_id)
        refreshed = await self._configs.get(tenant_id)
        metrics.record_operation("activate", "ok" if report.ok else "partial")
        return (refreshed or config), report

    async def disable_external_kms(self, tenant_id: int) -> RewrapReport:
        """Move every DEK back to the platform baseline, then drop the BYOK config.

        The explicit exit ramp: never entitlement-gated. The config is removed
        only if *every* DEK was re-wrapped; if the customer key is unreachable
        or revoked the report lists the versions still under it and the config
        is kept so they remain addressable.
        """
        validate_tenant_id(tenant_id)
        state = self._state(tenant_id)
        async with state.lock:
            report = await self._rewrap_rows(tenant_id, self._platform())
            if report.ok:
                await self._configs.delete(tenant_id)
        if report.ok:
            self.invalidate(tenant_id)
        metrics.record_operation("disable", "ok" if report.ok else "partial")
        return report
