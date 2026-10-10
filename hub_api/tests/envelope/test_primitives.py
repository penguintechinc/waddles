"""Pure primitives: AEAD slot binding, wrap blobs, wire form, platform KEK."""

from __future__ import annotations

import os
import uuid

import pytest

from services.envelope.crypto import (
    derive_data_subkey,
    field_aad,
    kek_id,
    local_unwrap,
    local_wrap,
    local_wrap_key_id,
    open_sealed,
    seal,
    validate_tenant_id,
    wrap_context,
)
from services.envelope.errors import (
    EnvelopeInputError,
    EnvelopeIntegrityError,
    KmsRejectedError,
    PlatformKekError,
)
from services.envelope.models import (
    DekRecord,
    EncryptedField,
    RewrapReport,
    is_envelope_wire,
)
from services.envelope.platform_kek import (
    CURRENT_ENV,
    PREVIOUS_ENV,
    LazyPlatformKek,
    PlatformKekAdapter,
)

ROW = uuid.UUID("11111111-2222-3333-4444-555555555555")


class TestSubkeyAndAad:
    """Tenant separation is cryptographic, and the AAD cannot be shifted by delimiters."""

    def test_subkey_is_deterministic_and_tenant_separated(self) -> None:
        dek = os.urandom(32)
        assert derive_data_subkey(dek, 1) == derive_data_subkey(dek, 1)
        assert derive_data_subkey(dek, 1) != derive_data_subkey(dek, 2)
        assert derive_data_subkey(dek, 1) != dek

    @pytest.mark.parametrize("size", [0, 16, 31, 33, 64])
    def test_subkey_requires_a_32_byte_dek(self, size: int) -> None:
        with pytest.raises(EnvelopeInputError):
            derive_data_subkey(b"\0" * size, 1)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"table": "a|b"},  # delimiter injection
            {"table": "Upper"},
            {"table": ""},
            {"column": "x" * 80},
            {"column": "has space"},
            {"row_uuid": "not-a-uuid"},
            {"row_uuid": 12345},
            {"dek_version": 0},
            {"dek_version": True},
            {"tenant_id": 0},
        ],
    )
    def test_field_aad_rejects_anything_that_could_blur_field_boundaries(self, kwargs) -> None:
        base = {"tenant_id": 1, "table": "t", "column": "c", "row_uuid": ROW, "dek_version": 1}
        with pytest.raises(EnvelopeInputError):
            field_aad(**{**base, **kwargs})

    def test_field_aad_is_stable_and_accepts_uuid_strings(self) -> None:
        assert field_aad(7, "t", "c", ROW, 3) == field_aad(7, "t", "c", str(ROW), 3)
        assert field_aad(7, "t", "c", ROW, 3) == f"7|t|c|{ROW}|3".encode()

    @pytest.mark.parametrize("bad", [0, -4, True, "1", 1.5])
    def test_validate_tenant_id(self, bad) -> None:
        with pytest.raises(EnvelopeInputError):
            validate_tenant_id(bad)

    def test_wrap_context_binds_tenant_and_purpose(self) -> None:
        assert wrap_context(9) == {"waddles_purpose": "tenant-dek", "waddles_tenant_id": "9"}


class TestSealOpen:
    """AES-256-GCM with a fresh nonce per call and AAD authentication."""

    def test_round_trip_and_aad_binding(self) -> None:
        key = os.urandom(32)
        iv, ct = seal(key, b"secret", b"aad-1")
        assert open_sealed(key, iv, ct, b"aad-1") == b"secret"
        with pytest.raises(EnvelopeIntegrityError):
            open_sealed(key, iv, ct, b"aad-2")

    def test_nonces_do_not_repeat(self) -> None:
        key = os.urandom(32)
        nonces = {seal(key, b"x", b"a")[0] for _ in range(3000)}
        assert len(nonces) == 3000

    def test_wrong_key_and_bad_nonce_are_integrity_failures(self) -> None:
        key = os.urandom(32)
        iv, ct = seal(key, b"x", b"a")
        with pytest.raises(EnvelopeIntegrityError):
            open_sealed(os.urandom(32), iv, ct, b"a")
        with pytest.raises(EnvelopeIntegrityError):
            open_sealed(key, b"short", ct, b"a")


class TestLocalWrap:
    """The platform baseline wrap: context-bound AES-GCM with a rotatable key id header."""

    def test_round_trip_and_context_binding(self) -> None:
        kek, dek = os.urandom(32), os.urandom(32)
        blob = local_wrap(kek, dek, wrap_context(1))
        assert local_unwrap(kek, blob, wrap_context(1)) == dek
        with pytest.raises(EnvelopeIntegrityError):
            local_unwrap(kek, blob, wrap_context(2))
        with pytest.raises(EnvelopeIntegrityError):
            local_unwrap(os.urandom(32), blob, wrap_context(1))

    def test_blob_carries_a_non_reversible_kek_id(self) -> None:
        kek = os.urandom(32)
        blob = local_wrap(kek, os.urandom(32), wrap_context(1))
        assert local_wrap_key_id(blob) == kek_id(kek)
        assert len(kek_id(kek)) == 16 and kek.hex() not in kek_id(kek)

    @pytest.mark.parametrize("blob", [b"", b"\x02" + b"\0" * 80, b"\x01short"])
    def test_malformed_blobs_are_rejected(self, blob: bytes) -> None:
        with pytest.raises(EnvelopeInputError):
            local_wrap_key_id(blob)

    def test_key_sizes_are_enforced(self) -> None:
        with pytest.raises(EnvelopeInputError):
            local_wrap(b"short", os.urandom(32), wrap_context(1))
        with pytest.raises(EnvelopeInputError):
            local_wrap(os.urandom(32), b"short", wrap_context(1))

    def test_unwrap_rejects_a_payload_of_the_wrong_length(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from services.envelope.crypto import context_aad

        kek = os.urandom(32)
        iv = os.urandom(12)
        sealed = AESGCM(kek).encrypt(iv, b"only-16-bytes!!!", context_aad(wrap_context(1)))
        blob = b"\x01" + bytes.fromhex(kek_id(kek)) + iv + sealed
        with pytest.raises(EnvelopeIntegrityError, match="unexpected length"):
            local_unwrap(kek, blob, wrap_context(1))


class TestWireForm:
    """The single-column ``wenv1.<version>.<blob>`` form."""

    def test_round_trip(self) -> None:
        original = EncryptedField(dek_version=4, iv=os.urandom(12), ciphertext=os.urandom(40))
        parsed = EncryptedField.from_wire(original.to_wire())
        assert parsed == original
        assert is_envelope_wire(original.to_wire())
        assert not is_envelope_wire("Zm9vYmFy")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "wenv2.1.AAAA",
            "wenv1.1",
            "wenv1.x.AAAA",
            "wenv1.0." + "A" * 60,
            "wenv1.1.AAAA",  # too short for nonce + tag
            "wenv1.1.éé",  # non-ascii
            "wenv1.1.!!!!",
        ],
    )
    def test_malformed_wire_values_are_rejected(self, value: str) -> None:
        with pytest.raises(EnvelopeInputError):
            EncryptedField.from_wire(value)


class TestReprSafety:
    """A stray ``log.debug(obj)`` can never emit key or ciphertext bytes."""

    def test_key_bearing_fields_are_excluded_from_repr(self) -> None:
        record = DekRecord(
            id=1,
            tenant_id=1,
            dek_version=1,
            wrapped_dek=b"WRAPPED-SECRET-BYTES",
            kek_kind="platform",
            kek_ref="platform:abc",
            status="active",
            usage_count=0,
            activated_at=None,
        )
        assert "WRAPPED" not in repr(record)
        field = EncryptedField(1, b"IV-BYTES-12345", b"CIPHERTEXT-BYTES")
        assert "CIPHERTEXT" not in repr(field) and "IV-BYTES" not in repr(field)


class TestRewrapReport:
    """``ok`` means every version is on the target KEK, no more and no less."""

    @pytest.mark.parametrize(
        ("rewrapped", "current", "failed", "total", "ok"),
        [(2, 1, (), 3, True), (1, 1, (2,), 3, False), (1, 1, (), 3, False), (0, 0, (), 0, True)],
    )
    def test_ok(self, rewrapped, current, failed, total, ok) -> None:
        report = RewrapReport(1, "customer_kms", "k", total, rewrapped, current, failed)
        assert report.ok is ok


class TestPlatformKek:
    """The baseline KEK: strict parsing, rotation, loud failure."""

    def test_from_env_requires_a_valid_key(self) -> None:
        with pytest.raises(PlatformKekError, match="not set"):
            PlatformKekAdapter.from_env({})
        with pytest.raises(PlatformKekError, match="64 hex"):
            PlatformKekAdapter.from_env({CURRENT_ENV: "abcd"})
        with pytest.raises(PlatformKekError, match="hex-encoded"):
            PlatformKekAdapter.from_env({CURRENT_ENV: "zz" * 32})
        with pytest.raises(PlatformKekError, match="32 bytes"):
            PlatformKekAdapter(b"short")

    def test_error_messages_never_echo_the_key(self) -> None:
        secret = "zz" * 32
        with pytest.raises(PlatformKekError) as raised:
            PlatformKekAdapter.from_env({CURRENT_ENV: secret})
        assert secret not in str(raised.value)

    async def test_rotation_unwraps_with_previous_and_rewraps_onto_current(self) -> None:
        old, new = os.urandom(32), os.urandom(32)
        dek, ctx = os.urandom(32), wrap_context(1)
        wrapped_old = await PlatformKekAdapter(old).wrap(dek, context=ctx)
        rotated = PlatformKekAdapter.from_env({CURRENT_ENV: new.hex(), PREVIOUS_ENV: old.hex()})
        assert await rotated.unwrap(wrapped_old, context=ctx) == dek
        assert local_wrap_key_id(await rotated.wrap(dek, context=ctx)) == kek_id(new)
        # Once PREVIOUS is dropped, the old wrap is refused loudly rather than guessed at.
        with pytest.raises(KmsRejectedError, match="not loaded"):
            await PlatformKekAdapter(new).unwrap(wrapped_old, context=ctx)

    async def test_a_non_platform_blob_is_rejected(self) -> None:
        adapter = PlatformKekAdapter(os.urandom(32))
        with pytest.raises(KmsRejectedError, match="not a platform-KEK wrap"):
            await adapter.unwrap(b'{"v":1}', context=wrap_context(1))

    async def test_verify_and_identity(self) -> None:
        adapter = PlatformKekAdapter(os.urandom(32))
        info = await adapter.verify()
        assert (info.provider, info.key_state) == ("platform", "Enabled")
        assert adapter.kek_kind == "platform" and adapter.key_ref.startswith("platform:")

    def test_lazy_kek_defers_and_does_not_cache_failures(self) -> None:
        calls = {"n": 0}

        def factory() -> PlatformKekAdapter:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PlatformKekError("not mounted yet")
            return PlatformKekAdapter(os.urandom(32))

        lazy = LazyPlatformKek(factory)
        assert calls["n"] == 0  # constructing it never touches the environment
        with pytest.raises(PlatformKekError):
            lazy.get()
        first = lazy.get()  # the failure was not cached
        assert lazy.get() is first and calls["n"] == 2
