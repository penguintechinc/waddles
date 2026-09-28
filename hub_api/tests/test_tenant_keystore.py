"""Tenant-DEK broker tests -- wrap/unwrap, cross-tenant isolation, rotation, shredding."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.tenant_keystore import (
    USAGE_CAP,
    CustomerKmsKekProvider,
    K8sSecretKekProvider,
    TenantDekRecord,
    TenantKeyNotFound,
    TenantKeyShredded,
    TenantKeystore,
)

_TEST_KEK_HEX = "b" * 64  # 32 bytes hex


@pytest.fixture(autouse=True)
def _kek_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_KEK_HEX", _TEST_KEK_HEX)


class FakeKeystoreRepository:
    """In-memory `KeystoreRepository` -- one dict per tenant, list of versions."""

    def __init__(self) -> None:
        """Start with no key rows and no tombstones for any tenant."""
        self._rows: dict[tuple[int, int], TenantDekRecord] = {}
        self._next_id = 1
        self._tombstones: set[int] = set()

    async def get_active(self, tenant_id: int) -> TenantDekRecord | None:
        candidates = [
            r for (t, _v), r in self._rows.items() if t == tenant_id and r.status == "active"
        ]
        return max(candidates, key=lambda r: r.dek_version) if candidates else None

    async def get_version(self, tenant_id: int, dek_version: int) -> TenantDekRecord | None:
        return self._rows.get((tenant_id, dek_version))

    async def insert_active(
        self, tenant_id: int, wrapped_dek: bytes, kek_ref: str, kek_kind: str
    ) -> TenantDekRecord:
        existing_versions = [v for (t, v) in self._rows if t == tenant_id]
        version = max(existing_versions, default=0) + 1
        record = TenantDekRecord(
            id=self._next_id,
            tenant_id=tenant_id,
            dek_version=version,
            wrapped_dek=wrapped_dek,
            kek_ref=kek_ref,
            kek_kind=kek_kind,
            status="active",
            usage_count=0,
            activated_at=datetime.now(UTC),
        )
        self._next_id += 1
        self._rows[(tenant_id, version)] = record
        return record

    async def retire(self, tenant_id: int, dek_version: int) -> None:
        record = self._rows[(tenant_id, dek_version)]
        self._rows[(tenant_id, dek_version)] = replace(
            record, status="retired", retired_at=datetime.now(UTC)
        )

    async def increment_usage(self, tenant_id: int, dek_version: int) -> int:
        record = self._rows[(tenant_id, dek_version)]
        new_count = record.usage_count + 1
        self._rows[(tenant_id, dek_version)] = replace(record, usage_count=new_count)
        return new_count

    async def destroy_all(self, tenant_id: int) -> None:
        for key, record in list(self._rows.items()):
            if key[0] == tenant_id:
                self._rows[key] = replace(
                    record,
                    status="destroyed",
                    wrapped_dek=None,
                    destroyed_at=datetime.now(UTC),
                )

    async def add_tombstone(self, tenant_id: int, dek_version: int, reason: str) -> None:
        self._tombstones.add(tenant_id)

    async def is_shredded(self, tenant_id: int) -> bool:
        return tenant_id in self._tombstones


@pytest.fixture
def keystore() -> TenantKeystore:
    return TenantKeystore(repository=FakeKeystoreRepository(), kek_provider=K8sSecretKekProvider())


async def test_wrap_unwrap_round_trip(keystore: TenantKeystore) -> None:
    """A freshly created tenant key unwraps back to a usable 32-byte DEK."""
    created = await keystore.create_tenant_key(tenant_id=1)
    dek, record = await keystore.get_dek(tenant_id=1)

    assert len(dek) == 32
    assert record.dek_version == created.dek_version == 1
    assert record.wrapped_dek is not None
    assert dek not in record.wrapped_dek  # ciphertext never contains the raw key bytes


async def test_cross_tenant_unwrap_denied(keystore: TenantKeystore) -> None:
    """A tenant's wrapped DEK fails to unwrap under a different tenant's AAD context."""
    await keystore.create_tenant_key(tenant_id=1)
    record = await keystore.repository.get_active(1)
    assert record is not None and record.wrapped_dek is not None

    with pytest.raises(Exception):  # noqa: PT011, B017 -- cryptography raises InvalidTag
        await keystore.kek_provider.unwrap(2, record.wrapped_dek)


async def test_shredded_tenant_raises(keystore: TenantKeystore) -> None:
    """`shred()` makes every subsequent `get_dek()` raise `TenantKeyShredded` (maps to 410)."""
    await keystore.create_tenant_key(tenant_id=1)
    await keystore.shred(tenant_id=1)

    with pytest.raises(TenantKeyShredded):
        await keystore.get_dek(tenant_id=1)


async def test_no_key_returns_not_found(keystore: TenantKeystore) -> None:
    with pytest.raises(TenantKeyNotFound):
        await keystore.get_dek(tenant_id=999)


async def test_rotation_keeps_old_version_decryptable(keystore: TenantKeystore) -> None:
    """After rotation, the OLD dek_version is still fetchable and unwraps correctly."""
    await keystore.create_tenant_key(tenant_id=1)
    old_dek, old_record = await keystore.get_dek(tenant_id=1)

    new_record = await keystore.rotate(tenant_id=1)
    assert new_record.dek_version == old_record.dek_version + 1

    # New default (active) fetch returns the new version.
    new_dek, fetched_new = await keystore.get_dek(tenant_id=1)
    assert fetched_new.dek_version == new_record.dek_version
    assert new_dek != old_dek

    # Explicitly requesting the old version still decrypts it (spec Sec4:
    # "existing ciphertext must stay decryptable").
    fetched_old_dek, fetched_old_record = await keystore.get_dek(
        tenant_id=1, version=old_record.dek_version
    )
    assert fetched_old_record.status == "retired"
    assert fetched_old_dek == old_dek


async def test_usage_cap_triggers_auto_rotation(keystore: TenantKeystore) -> None:
    """Hitting `USAGE_CAP` on the active version auto-rotates -- next fetch is a new version."""
    await keystore.create_tenant_key(tenant_id=1)
    repo = keystore.repository
    assert isinstance(repo, FakeKeystoreRepository)
    # Fast-forward the counter to one below the cap instead of looping
    # USAGE_CAP times.
    record = await repo.get_active(1)
    assert record is not None
    repo._rows[(1, record.dek_version)] = replace(record, usage_count=USAGE_CAP - 1)

    _, fetched = await keystore.get_dek(tenant_id=1)
    assert fetched.dek_version == 1  # this call still used version 1

    active = await repo.get_active(1)
    assert active is not None
    assert active.dek_version == 2  # auto-rotated for the *next* caller


def test_no_key_material_in_record_repr() -> None:
    """`TenantDekRecord` never carries an unwrapped DEK -- only the wrapped ciphertext."""
    record = TenantDekRecord(
        id=1,
        tenant_id=1,
        dek_version=1,
        wrapped_dek=b"\x00" * 44,
        kek_ref="k8s-secret:TENANT_KEK_HEX",
        kek_kind="platform",
        status="active",
        usage_count=0,
        activated_at=datetime.now(UTC),
    )
    # The only bytes field is the WRAPPED (ciphertext) dek -- there is no
    # plaintext-DEK field on this dataclass at all, so no repr/log call
    # site can ever accidentally print unwrapped key material from it.
    assert not hasattr(record, "dek")
    assert not hasattr(record, "plaintext_dek")


class _FakeKmsClient:
    """Minimal fake matching `AwsKmsKek`'s `_KmsClient` structural protocol.

    Enforces `EncryptionContext` equality between `encrypt`/`decrypt`, same
    as real AWS KMS, so `CustomerKmsKekProvider`'s cross-tenant-context
    rejection is exercised without a live AWS account.
    """

    def __init__(self, key_arn: str = "arn:aws:kms:us-east-1:123456789012:key/fake") -> None:
        self._key_arn = key_arn

    def describe_key(self, *, KeyId: str) -> dict[str, object]:  # noqa: N803
        return {"KeyMetadata": {"Arn": self._key_arn}}

    def encrypt(
        self,
        *,
        KeyId: str,  # noqa: N803
        Plaintext: bytes,  # noqa: N803
        EncryptionContext: dict[str, str],  # noqa: N803
    ) -> dict[str, object]:
        blob = Plaintext.hex().encode() + b"::" + repr(EncryptionContext).encode()
        return {"CiphertextBlob": blob}

    def decrypt(
        self,
        *,
        CiphertextBlob: bytes,  # noqa: N803
        KeyId: str,  # noqa: N803
        EncryptionContext: dict[str, str],  # noqa: N803
    ) -> dict[str, object]:
        pt_hex, _sep, stored_ctx_repr = CiphertextBlob.partition(b"::")
        if stored_ctx_repr != repr(EncryptionContext).encode():
            raise ValueError("EncryptionContext mismatch -- simulated KMS InvalidCiphertext")
        return {"KeyId": self._key_arn, "Plaintext": bytes.fromhex(pt_hex.decode())}


async def test_customer_kms_kek_round_trip() -> None:
    """`CustomerKmsKekProvider` wraps/unwraps via the (fake) `AwsKmsKek` client."""
    provider = CustomerKmsKekProvider(key_id="alias/tenant-kek", kms_client=_FakeKmsClient())
    dek = b"\x02" * 32
    wrapped = await provider.wrap(1, dek)
    assert wrapped != dek
    assert await provider.unwrap(1, wrapped) == dek


async def test_customer_kms_kek_cross_tenant_denied() -> None:
    """A DEK wrapped for tenant 1 fails to unwrap under tenant 2's context."""
    provider = CustomerKmsKekProvider(key_id="alias/tenant-kek", kms_client=_FakeKmsClient())
    wrapped = await provider.wrap(1, b"\x03" * 32)
    with pytest.raises(ValueError, match="EncryptionContext mismatch"):
        await provider.unwrap(2, wrapped)


def test_customer_kms_kek_ref_is_key_id() -> None:
    provider = CustomerKmsKekProvider(key_id="arn:aws:kms:us-east-1:123456789012:key/fake")
    assert provider.ref(tenant_id=1) == "arn:aws:kms:us-east-1:123456789012:key/fake"
