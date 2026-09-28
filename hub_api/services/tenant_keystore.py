"""Per-tenant DEK broker -- envelope encryption key lifecycle (keystore schema).

Implements the key-lifecycle half of `docs/superpowers/specs/
2026-09-28-tenant-envelope-encryption-design.md` Sec4: DEK generation,
KEK-wrap/unwrap, versioning, the 2^30-operation nonce-cap auto-rotation
trigger, and crypto-shred tombstoning. Storage is behind the
`KeystoreRepository` protocol so this module has no direct DB driver
dependency and is unit-testable with a fake in-memory repository (see
`tests/test_tenant_keystore.py`); `AsyncpgKeystoreRepository` is the real
Postgres-backed implementation used by `app.py` at startup, pointed at
the dedicated `keystore` schema/role from migration 0035.

**Crypto primitive note:** KEK wrap/unwrap uses `penguin_security.crypto.
envelope.kek`'s providers (`LocalSecretKek` for the platform baseline,
`AwsKmsKek` for Enterprise BYOK) -- both are real, fully implemented in
`penguin-security[crypto]==0.1.1` (confirmed on PyPI: `provides_extra:
['crypto', ...]`, a real `envelope/kek.py` in the wheel; the local
`~/code/penguin-libs` checkout used during initial development was simply
on an older branch that predates this module). `K8sSecretKekProvider`/
`CustomerKmsKekProvider` below are thin adapters from this module's own
async `KekProvider` protocol (used throughout `TenantKeystore`) onto
`penguin_security`'s sync, `context`-keyword-based provider protocol --
the two protocols differ in call shape (async vs sync, positional
`tenant_id` vs a `context: Mapping[str, str]` binding dict), not in
capability, so no raw-primitive fallback was needed for either provider.
`asyncio.to_thread` wraps every call since `AwsKmsKek` does real network
I/O (KMS) and `LocalSecretKek`, while CPU-only, shares the same call
shape -- consistent async-safety over a micro-optimization for the local
case.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from penguin_security.crypto.envelope.kek import AwsKmsKek, LocalSecretKek

#: NIST SP 800-38D random-96-bit-nonce collision bound -- see spec Sec2.
#: Hitting this on the `at-rest` purpose's `dek_version` auto-triggers
#: rotation (spec Sec4). The `ingest-stream` purpose uses its own, much
#: lower cap -- see `STREAM_USAGE_CAP` below (spec Sec5a).
USAGE_CAP = 2**30

_DEK_LENGTH_BYTES = 32  # AES-256

#: `keystore.tenant_encryption_keys.purpose` values (spec Sec5a amendment).
#: `AT_REST` never leaves hub-api, in plaintext or wrapped form, under any
#: circumstance (spec Sec5 intro, unchanged). `STREAM` is the one purpose
#: this module's broker (`blueprints/v1/internal_keys.py`) is allowed to
#: distribute at all, sealed per spec Sec5b -- never `AT_REST`.
PURPOSE_AT_REST = "at-rest"
PURPOSE_INGEST_STREAM = "ingest-stream"

#: Server-side purpose -> allowed-service-identity mapping (spec Sec5a).
#: Deliberately a fixed in-code mapping, never derived from the request
#: body or the JWT's raw `scope` string -- a forged/over-broad scope on a
#: compromised caller still can't obtain a purpose it isn't listed here
#: for. Capability ("encrypt"/"decrypt") is documented intent, enforced
#: procedurally (audit + anomaly metric), not cryptographically -- see
#: spec Sec5a's note on why AES-256-GCM has no asymmetric encrypt/decrypt
#: split to enforce it with.
PURPOSE_SERVICE_CAPABILITIES: dict[str, dict[str, frozenset[str]]] = {
    PURPOSE_INGEST_STREAM: {
        "svc-ingest": frozenset({"encrypt"}),
        "svc-process": frozenset({"decrypt"}),
    },
}

#: spec Sec5a: usage-count backstop for `ingest-stream`, far below the
#: at-rest DEK's 2^30 nonce-collision bound -- 2^24 = 16,777,216 ops.
#: Math: even a single tenant sustaining an anomalous 1,000 events/sec
#: exhausts this in ~4.7 hours, comfortably inside the 24h time-based
#: rotation window below, so this is a backstop, not the primary trigger.
STREAM_USAGE_CAP = 2**24

#: spec Sec5a: primary rotation trigger for `ingest-stream` -- wall-clock,
#: not usage-based (reverse of the at-rest DEK's design, see spec Sec5f).
STREAM_ROTATION_INTERVAL_SECONDS = 24 * 60 * 60

#: spec Sec5a: a retired `ingest-stream` version stays fetchable/
#: decryptable for this long after retirement, covering in-flight events
#: and consumer-side cache lag straddling a rotation boundary.
STREAM_GRACE_WINDOW_SECONDS = 2 * STREAM_ROTATION_INTERVAL_SECONDS


def is_service_allowed(purpose: str, service_id: str) -> bool:
    """Server-side purpose->service check (spec Sec5a) -- never client-supplied."""
    return service_id in PURPOSE_SERVICE_CAPABILITIES.get(purpose, {})


class TenantKeyError(Exception):
    """Base error for tenant-DEK-broker failures."""


class TenantKeyNotFound(TenantKeyError):  # noqa: N818 - contract-pinned name, matches spec vocabulary
    """No active key exists for this tenant (or requested version)."""


class TenantKeyShredded(TenantKeyError):  # noqa: N818 - contract-pinned name, matches spec vocabulary
    """Tenant's key has been crypto-shredded -- tombstoned, permanently gone.

    Callers (the internal API blueprint) map this to HTTP 410 Gone, never
    404 -- a shredded tenant is a deliberate, permanent, irreversible
    state, distinct from "never had a key."
    """


@dataclass(slots=True, frozen=True)
class TenantDekRecord:
    """One row of `keystore.tenant_encryption_keys` -- never holds an unwrapped DEK."""

    id: int
    tenant_id: int
    purpose: str
    dek_version: int
    wrapped_dek: bytes | None
    kek_ref: str
    kek_kind: str
    status: str
    usage_count: int
    activated_at: datetime
    retired_at: datetime | None = None
    destroyed_at: datetime | None = None


class KeystoreRepository(Protocol):
    """Storage boundary for `keystore.tenant_encryption_keys`/`key_tombstones`.

    Kept minimal and storage-agnostic so `TenantKeystore` never imports a
    DB driver directly -- `AsyncpgKeystoreRepository` (below) is the real
    implementation; tests use an in-memory fake of the same shape.
    """

    async def get_active(
        self, tenant_id: int, *, purpose: str = PURPOSE_AT_REST
    ) -> TenantDekRecord | None:
        """Return the current `active`-status key row for `(tenant_id, purpose)`, or `None`."""
        ...

    async def get_version(
        self, tenant_id: int, dek_version: int, *, purpose: str = PURPOSE_AT_REST
    ) -> TenantDekRecord | None:
        """Return the specific `(tenant_id, purpose, dek_version)` row, or `None`."""
        ...

    async def insert_active(
        self,
        tenant_id: int,
        wrapped_dek: bytes,
        kek_ref: str,
        kek_kind: str,
        *,
        purpose: str = PURPOSE_AT_REST,
    ) -> TenantDekRecord:
        """Insert a new `active` key row, auto-assigning the next `dek_version` for `purpose`."""
        ...

    async def retire(
        self, tenant_id: int, dek_version: int, *, purpose: str = PURPOSE_AT_REST
    ) -> None:
        """Flip a key row from `active` to `retired` (kept, not destroyed)."""
        ...

    async def increment_usage(
        self, tenant_id: int, dek_version: int, *, purpose: str = PURPOSE_AT_REST
    ) -> int:
        """Atomically bump the usage counter; return the new count."""
        ...

    async def destroy_all(self, tenant_id: int) -> None:
        """Mark every key row for `tenant_id` `destroyed`, wiping `wrapped_dek`."""
        ...

    async def add_tombstone(self, tenant_id: int, dek_version: int, reason: str) -> None:
        """Append a durable, permanent crypto-shred record."""
        ...

    async def is_shredded(self, tenant_id: int) -> bool:
        """Return whether `tenant_id` has any tombstone entry at all."""
        ...


class KekProvider(Protocol):
    """Root-KEK abstraction.

    Platform k8s-Secret key (alpha/baseline) or pluggable customer KMS
    (Enterprise BYOK, spec Sec6).
    """

    kek_kind: str

    def ref(self, tenant_id: int) -> str:
        """Return an opaque identifier for the KEK used to wrap `tenant_id`'s DEK."""
        ...

    async def wrap(self, tenant_id: int, plaintext_dek: bytes) -> bytes:
        """Wrap `plaintext_dek` under the root KEK, bound to `tenant_id`."""
        ...

    async def unwrap(self, tenant_id: int, wrapped_dek: bytes) -> bytes:
        """Unwrap `wrapped_dek`; raises if it was wrapped for a different tenant."""
        ...


@dataclass(slots=True)
class K8sSecretKekProvider:
    """Platform-baseline KEK: a single 256-bit key mounted from a k8s Secret.

    Adapts `penguin_security.crypto.envelope.kek.LocalSecretKek` (hex-
    encoded key material, per `scripts/generate-tenant-kek.sh`'s output)
    onto this module's async `KekProvider` protocol. `LocalSecretKek`
    itself binds `context={"tenant_id": ...}` as its AES-GCM AAD (spec
    Sec2's AAD discipline, applied one level up the hierarchy), so a
    wrapped DEK copied to another tenant's row fails to unwrap rather
    than silently succeeding under the wrong tenant.
    """

    kek_hex_env_var: str = "TENANT_KEK_HEX"
    kek_kind: str = field(default="platform", init=False)

    def _provider(self) -> LocalSecretKek:
        try:
            return LocalSecretKek(env_var=self.kek_hex_env_var, encoding="hex")
        except ValueError as exc:
            raise TenantKeyError(str(exc)) from exc

    def ref(self, tenant_id: int) -> str:
        """Return the k8s-Secret env var name this KEK is sourced from."""
        return f"k8s-secret:{self.kek_hex_env_var}"

    async def wrap(self, tenant_id: int, plaintext_dek: bytes) -> bytes:
        """Wrap `plaintext_dek` via `LocalSecretKek`, context-bound to `tenant_id`."""
        provider = self._provider()
        return await asyncio.to_thread(
            provider.wrap, plaintext_dek, context={"tenant_id": str(tenant_id)}
        )

    async def unwrap(self, tenant_id: int, wrapped_dek: bytes) -> bytes:
        """Reverse `wrap()`; raises on a tenant-context mismatch (cross-tenant swap)."""
        provider = self._provider()
        return await asyncio.to_thread(
            provider.unwrap, wrapped_dek, context={"tenant_id": str(tenant_id)}
        )


@dataclass(slots=True)
class CustomerKmsKekProvider:
    """Enterprise BYOK KEK (spec Sec6) -- adapts `penguin_security`'s `AwsKmsKek`.

    One instance is bound to one tenant's customer-owned KMS key ARN
    (`kek_ref` in `tenant_encryption_keys`) at construction -- callers
    (e.g. a future `@license_required("enterprise")`-gated key-provisioning
    path) construct a `CustomerKmsKekProvider(key_id=customer_arn)` per
    tenant rather than sharing one instance across tenants, since
    `AwsKmsKek` itself is bound to a single `key_id`. `AwsKmsKek` already
    enforces the `EncryptionContext=tenant_id` binding natively via KMS
    (spec Sec6) -- not re-implemented here.
    """

    key_id: str
    region_name: str | None = None
    #: Injectable `boto3` KMS client -- production leaves this `None` (real
    #: client, `AwsKmsKek`'s own default); tests inject a fake matching
    #: `AwsKmsKek`'s `_KmsClient` structural protocol so this class is
    #: unit-testable without a live AWS account.
    kms_client: Any | None = None
    kek_kind: str = field(default="customer_kms", init=False)
    _provider: AwsKmsKek | None = field(default=None, init=False, repr=False)

    def _get_provider(self) -> AwsKmsKek:
        # Cached after first construction -- `AwsKmsKek.__init__` makes a
        # `kms:DescribeKey` call to resolve the canonical key ARN, so
        # rebuilding it per wrap/unwrap would add a network round-trip to
        # every call for no benefit.
        if self._provider is None:
            self._provider = AwsKmsKek(
                self.key_id, client=self.kms_client, region_name=self.region_name
            )
        return self._provider

    def ref(self, tenant_id: int) -> str:
        """Return the bound customer KMS key id/ARN."""
        return self.key_id

    async def wrap(self, tenant_id: int, plaintext_dek: bytes) -> bytes:
        """Wrap `plaintext_dek` via `kms:Encrypt`, context-bound to `tenant_id`."""
        provider = await asyncio.to_thread(self._get_provider)
        return await asyncio.to_thread(
            provider.wrap, plaintext_dek, context={"tenant_id": str(tenant_id)}
        )

    async def unwrap(self, tenant_id: int, wrapped_dek: bytes) -> bytes:
        """Unwrap via `kms:Decrypt`, requiring the same `tenant_id` context used to wrap."""
        provider = await asyncio.to_thread(self._get_provider)
        return await asyncio.to_thread(
            provider.unwrap, wrapped_dek, context={"tenant_id": str(tenant_id)}
        )


#: One invalidation event published on rotation/shred (spec Sec5c) --
#: `TenantKeystore`'s caller (app.py wiring) supplies the actual
#: publish-to-Valkey callback; this module stays transport-agnostic and
#: unit-testable without a live Valkey connection.
InvalidationPublisher = Any  # Callable[[int, str, int, str], Awaitable[None]]


@dataclass(slots=True)
class TenantKeystore:
    """The tenant-DEK broker: create, fetch (and unwrap), rotate, shred.

    Never logs or returns a wrapped/unwrapped DEK's bytes on any error
    path -- callers (blueprint layer) are responsible for the same
    discipline in audit-log calls (never pass key material as a field).
    """

    repository: KeystoreRepository
    kek_provider: KekProvider
    #: Called `(tenant_id, purpose, old_dek_version, reason)` after every
    #: rotation/shred -- publishes `keys:tenant-dek:invalidate` (spec
    #: Sec5c). `None` (default) skips publishing -- used by every existing
    #: at-rest-only test/caller that predates this contract.
    invalidation_publisher: InvalidationPublisher | None = None

    async def create_tenant_key(
        self, tenant_id: int, *, purpose: str = PURPOSE_AT_REST
    ) -> TenantDekRecord:
        """Generate + wrap a new v1 DEK for `(tenant_id, purpose)`. Called at tenant creation."""
        plaintext_dek = secrets.token_bytes(_DEK_LENGTH_BYTES)
        wrapped = await self.kek_provider.wrap(tenant_id, plaintext_dek)
        return await self.repository.insert_active(
            tenant_id,
            wrapped,
            self.kek_provider.ref(tenant_id),
            self.kek_provider.kek_kind,
            purpose=purpose,
        )

    async def get_dek(
        self, tenant_id: int, *, purpose: str = PURPOSE_AT_REST, version: int | None = None
    ) -> tuple[bytes, TenantDekRecord]:
        """Return `(unwrapped_dek, record)` for `(tenant_id, purpose)`'s active (or `version`) key.

        Raises `TenantKeyShredded` if the tenant has been crypto-shredded,
        `TenantKeyNotFound` if no matching key row exists. Auto-rotates
        the moment this DEK version's usage counter reaches its purpose's
        cap (`USAGE_CAP` for `at-rest`, spec Sec2/Sec4; `STREAM_USAGE_CAP`
        for `ingest-stream`, spec Sec5a) **or**, for `ingest-stream` only,
        once `STREAM_ROTATION_INTERVAL_SECONDS` has elapsed since
        activation -- the caller still gets the DEK it asked for this
        call; the *next* caller gets the new version.
        """
        if await self.repository.is_shredded(tenant_id):
            raise TenantKeyShredded(f"tenant {tenant_id} key has been crypto-shredded")

        record = (
            await self.repository.get_version(tenant_id, version, purpose=purpose)
            if version is not None
            else await self.repository.get_active(tenant_id, purpose=purpose)
        )
        if record is None or record.wrapped_dek is None or record.status == "destroyed":
            raise TenantKeyNotFound(f"no {purpose} key for tenant {tenant_id} (version={version})")

        unwrapped = await self.kek_provider.unwrap(tenant_id, record.wrapped_dek)

        if record.status == "active":
            new_count = await self.repository.increment_usage(
                tenant_id, record.dek_version, purpose=purpose
            )
            usage_cap = STREAM_USAGE_CAP if purpose == PURPOSE_INGEST_STREAM else USAGE_CAP
            expired_by_age = (
                purpose == PURPOSE_INGEST_STREAM
                and _age_seconds(record.activated_at) >= STREAM_ROTATION_INTERVAL_SECONDS
            )
            if new_count >= usage_cap or expired_by_age:
                await self.rotate(
                    tenant_id,
                    purpose=purpose,
                    reason="usage_cap" if new_count >= usage_cap else "scheduled_rotation",
                )

        return unwrapped, record

    async def rotate(
        self, tenant_id: int, *, purpose: str = PURPOSE_AT_REST, reason: str = "rotation"
    ) -> TenantDekRecord:
        """Mint a new active DEK version; retire (never delete) the prior active one.

        Old ciphertext/stream traffic encrypted under the retired version
        stays decryptable through its grace window (spec Sec4/Sec5a) --
        `get_dek(tenant_id, purpose=purpose, version=<old>)` still works.
        Publishes an invalidation event (spec Sec5c) if a publisher is wired.
        """
        current = await self.repository.get_active(tenant_id, purpose=purpose)
        if current is not None:
            await self.repository.retire(tenant_id, current.dek_version, purpose=purpose)
        new_record = await self.create_tenant_key(tenant_id, purpose=purpose)
        if current is not None and self.invalidation_publisher is not None:
            await self.invalidation_publisher(tenant_id, purpose, current.dek_version, reason)
        return new_record

    async def shred(self, tenant_id: int, *, reason: str = "tenant_deleted") -> None:
        """Crypto-shred: destroy every key row (all purposes) for `tenant_id`; tombstone it.

        Irreversible by design (spec Sec4) -- every ciphertext row for
        this tenant becomes permanently unrecoverable the instant this
        returns, without touching the tenant's data rows at all. Publishes
        an invalidation event (spec Sec5c) for the `ingest-stream` purpose
        if a publisher is wired, so `svc_ingest`/`svc_process` drop their
        cache immediately rather than waiting out `max_cache_ttl_s`.
        """
        active_at_rest = await self.repository.get_active(tenant_id, purpose=PURPOSE_AT_REST)
        active_stream = await self.repository.get_active(tenant_id, purpose=PURPOSE_INGEST_STREAM)
        await self.repository.destroy_all(tenant_id)
        await self.repository.add_tombstone(
            tenant_id, active_at_rest.dek_version if active_at_rest is not None else 0, reason
        )
        if active_stream is not None and self.invalidation_publisher is not None:
            await self.invalidation_publisher(
                tenant_id, PURPOSE_INGEST_STREAM, active_stream.dek_version, reason
            )


def _age_seconds(activated_at: datetime) -> float:
    """Wall-clock seconds since `activated_at` (expected tz-aware, UTC -- asyncpg TIMESTAMPTZ)."""
    reference = (
        activated_at if activated_at.tzinfo is not None else activated_at.replace(tzinfo=UTC)
    )
    return (datetime.now(UTC) - reference).total_seconds()
