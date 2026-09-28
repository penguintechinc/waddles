"""Per-tenant DEK broker -- envelope encryption key lifecycle (keystore schema).

Implements the key-lifecycle half of `docs/superpowers/specs/
2026-09-28-tenant-envelope-encryption-design.md` Sec4: DEK generation,
KEK-wrap/unwrap, versioning, the 2^30-operation nonce-cap auto-rotation
trigger, and crypto-shred tombstoning. Storage is behind the
`KeystoreRepository` protocol so this module has no direct DB driver
dependency and is unit-testable with a fake in-memory repository (see
`tests/test_tenant_keystore.py`); `AsyncpgKeystoreRepository` is the real
Postgres-backed implementation used by `app.py` at startup, pointed at
the dedicated `keystore` schema/role from migration 0027.

**Crypto primitive note (blocking dependency, see PR description):** the
task spec calls for `penguin-security[crypto]`'s envelope primitives,
pinned exactly. As of this PR, `penguin-security` 0.1.0 (the version
actually present in `~/code/penguin-libs/packages/python-security`) ships
no `crypto` submodule, and no `0.1.1` build exists on PyPI -- `pip
download penguin-security==0.1.1` 404s. This module therefore uses
`cryptography`'s `AESGCM` directly (already a pinned hub-api dependency,
same primitive `services/bundle_secret_crypto.py` and
`services/platform_integrations_crypto.py` already use), behind
`KekProvider.wrap`/`unwrap` so swapping in `penguin_security.crypto` once
it ships is a one-file change, not a schema or API change.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: NIST SP 800-38D random-96-bit-nonce collision bound -- see spec Sec2.
#: Hitting this on a `dek_version` auto-triggers rotation (spec Sec4).
USAGE_CAP = 2**30

_DEK_LENGTH_BYTES = 32  # AES-256
_IV_LENGTH_BYTES = 12  # 96-bit GCM nonce, CSPRNG per encryption


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

    async def get_active(self, tenant_id: int) -> TenantDekRecord | None:
        """Return the current `active`-status key row for `tenant_id`, or `None`."""
        ...

    async def get_version(self, tenant_id: int, dek_version: int) -> TenantDekRecord | None:
        """Return the specific `dek_version` row for `tenant_id`, or `None`."""
        ...

    async def insert_active(
        self, tenant_id: int, wrapped_dek: bytes, kek_ref: str, kek_kind: str
    ) -> TenantDekRecord:
        """Insert a new `active` key row, auto-assigning the next `dek_version`."""
        ...

    async def retire(self, tenant_id: int, dek_version: int) -> None:
        """Flip a key row from `active` to `retired` (kept, not destroyed)."""
        ...

    async def increment_usage(self, tenant_id: int, dek_version: int) -> int:
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

    Generated by `scripts/generate-tenant-kek.sh`. Wraps each tenant DEK
    with AES-256-GCM, AAD-bound to the tenant id (spec Sec2's AAD
    discipline, applied one level up the hierarchy) so a wrapped DEK
    copied to another tenant's row fails to unwrap rather than silently
    succeeding under the wrong tenant.
    """

    kek_hex_env_var: str = "TENANT_KEK_HEX"
    kek_kind: str = field(default="platform", init=False)

    def _key(self) -> bytes:
        hex_key = os.environ.get(self.kek_hex_env_var, "")
        if len(hex_key) != _DEK_LENGTH_BYTES * 2:
            raise TenantKeyError(
                f"{self.kek_hex_env_var} must be a {_DEK_LENGTH_BYTES * 2}-character hex string"
            )
        return bytes.fromhex(hex_key)

    def ref(self, tenant_id: int) -> str:
        """Return the k8s-Secret env var name this KEK is sourced from."""
        return f"k8s-secret:{self.kek_hex_env_var}"

    async def wrap(self, tenant_id: int, plaintext_dek: bytes) -> bytes:
        """AES-256-GCM-wrap `plaintext_dek`; returns `iv || ciphertext_with_tag`."""
        iv = secrets.token_bytes(_IV_LENGTH_BYTES)
        aad = f"tenant-kek|{tenant_id}".encode()
        ciphertext = AESGCM(self._key()).encrypt(iv, plaintext_dek, aad)
        return iv + ciphertext

    async def unwrap(self, tenant_id: int, wrapped_dek: bytes) -> bytes:
        """Reverse `wrap()`; raises `cryptography.exceptions.InvalidTag` on AAD/tenant mismatch."""
        iv, ciphertext = wrapped_dek[:_IV_LENGTH_BYTES], wrapped_dek[_IV_LENGTH_BYTES:]
        aad = f"tenant-kek|{tenant_id}".encode()
        return AESGCM(self._key()).decrypt(iv, ciphertext, aad)


class CustomerKmsKekProvider:
    """Enterprise BYOK KEK (spec Sec6) -- not implemented in this PR.

    Placeholder so the `KekProvider` protocol has a named Enterprise
    branch point (`kek_kind='customer_kms'`) without gating this PR's
    baseline delivery on a full AWS KMS `EncryptionContext=tenant_id`
    integration + workload-identity-federated role, which is a separate,
    license-gated (`@license_required("enterprise")`) chunk of work per
    `critical-rules.md` Feature Flags & License Tiers.
    """

    kek_kind = "customer_kms"

    def ref(self, tenant_id: int) -> str:
        """Not implemented -- see class docstring."""
        raise NotImplementedError("Enterprise customer-KMS KEK not yet implemented")

    async def wrap(self, tenant_id: int, plaintext_dek: bytes) -> bytes:
        """Not implemented -- see class docstring."""
        raise NotImplementedError("Enterprise customer-KMS KEK not yet implemented")

    async def unwrap(self, tenant_id: int, wrapped_dek: bytes) -> bytes:
        """Not implemented -- see class docstring."""
        raise NotImplementedError("Enterprise customer-KMS KEK not yet implemented")


@dataclass(slots=True)
class TenantKeystore:
    """The tenant-DEK broker: create, fetch (and unwrap), rotate, shred.

    Never logs or returns a wrapped/unwrapped DEK's bytes on any error
    path -- callers (blueprint layer) are responsible for the same
    discipline in audit-log calls (never pass key material as a field).
    """

    repository: KeystoreRepository
    kek_provider: KekProvider

    async def create_tenant_key(self, tenant_id: int) -> TenantDekRecord:
        """Generate + wrap a new v1 DEK for `tenant_id`. Called at tenant creation."""
        plaintext_dek = secrets.token_bytes(_DEK_LENGTH_BYTES)
        wrapped = await self.kek_provider.wrap(tenant_id, plaintext_dek)
        return await self.repository.insert_active(
            tenant_id, wrapped, self.kek_provider.ref(tenant_id), self.kek_provider.kek_kind
        )

    async def get_dek(
        self, tenant_id: int, *, version: int | None = None
    ) -> tuple[bytes, TenantDekRecord]:
        """Return `(unwrapped_dek, record)` for `tenant_id`'s active (or `version`) key.

        Raises `TenantKeyShredded` if the tenant has been crypto-shredded,
        `TenantKeyNotFound` if no matching key row exists. Auto-rotates
        (spec Sec2/Sec4) the moment this DEK version's usage counter
        reaches `USAGE_CAP` -- the caller still gets the DEK it asked for
        this call; the *next* caller gets the new version.
        """
        if await self.repository.is_shredded(tenant_id):
            raise TenantKeyShredded(f"tenant {tenant_id} key has been crypto-shredded")

        record = (
            await self.repository.get_version(tenant_id, version)
            if version is not None
            else await self.repository.get_active(tenant_id)
        )
        if record is None or record.wrapped_dek is None or record.status == "destroyed":
            raise TenantKeyNotFound(f"no key for tenant {tenant_id} (version={version})")

        unwrapped = await self.kek_provider.unwrap(tenant_id, record.wrapped_dek)

        if record.status == "active":
            new_count = await self.repository.increment_usage(tenant_id, record.dek_version)
            if new_count >= USAGE_CAP:
                await self.rotate(tenant_id)

        return unwrapped, record

    async def rotate(self, tenant_id: int) -> TenantDekRecord:
        """Mint a new active DEK version; retire (never delete) the prior active one.

        Old ciphertext encrypted under the retired version stays
        decryptable indefinitely (spec Sec4) -- `get_dek(tenant_id,
        version=<old>)` still works after rotation.
        """
        current = await self.repository.get_active(tenant_id)
        if current is not None:
            await self.repository.retire(tenant_id, current.dek_version)
        return await self.create_tenant_key(tenant_id)

    async def shred(self, tenant_id: int, *, reason: str = "tenant_deleted") -> None:
        """Crypto-shred: destroy all key rows for `tenant_id`, tombstone permanently.

        Irreversible by design (spec Sec4) -- every ciphertext row for
        this tenant becomes permanently unrecoverable the instant this
        returns, without touching the tenant's data rows at all.
        """
        active = await self.repository.get_active(tenant_id)
        await self.repository.destroy_all(tenant_id)
        await self.repository.add_tombstone(
            tenant_id, active.dek_version if active is not None else 0, reason
        )
