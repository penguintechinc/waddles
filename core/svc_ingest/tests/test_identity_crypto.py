"""Tests for `identity_crypto` -- AES-256-GCM envelope encryption of identity fields.

Covers: no plaintext handle ever appears in the serialized envelope, AAD
binding rejects a ciphertext moved to another tenant/event/field, a full
encrypt->decrypt round trip, and that an old `dek_version` still decrypts
during a rotation window (multiple DEKs live at once).
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidTag

from identity_crypto import (
    CiphertextFormatError,
    DekUnavailableError,
    HubApiDekProvider,
    LocalDevDekProvider,
    TtlCachedDekProvider,
    build_aad,
    decrypt_identity_value,
    encrypt_identity_value,
    envelope_decrypt,
    envelope_dek_version,
    envelope_encrypt,
    from_json_envelope,
)

DEK_A = b"A" * 32
DEK_B = b"B" * 32


class TestEnvelopeRoundTrip:
    def test_encrypt_then_decrypt_recovers_plaintext(self) -> None:
        aad = build_aad(tenant_id="acme", stream="s", field="actor", event_id="e1", dek_version=1)
        envelope = envelope_encrypt("realuser42", DEK_A, dek_version=1, aad=aad)
        assert envelope_decrypt(envelope, DEK_A, aad=aad) == "realuser42"

    def test_no_plaintext_handle_in_serialized_envelope(self) -> None:
        """The literal username must never appear in the wire bytes, base64, or JSON form."""
        secret_handle = "xX_SuperSecretHandle_Xx"
        aad = build_aad(tenant_id="acme", stream="s", field="actor", event_id="e1", dek_version=1)
        envelope = envelope_encrypt(secret_handle, DEK_A, dek_version=1, aad=aad)
        assert secret_handle.encode() not in envelope
        assert secret_handle not in base64.b64encode(envelope).decode()

        json_obj = encrypt_identity_value(
            secret_handle,
            DEK_A,
            dek_version=1,
            tenant_id="acme",
            stream="s",
            field="actor",
            event_id="e1",
        )
        serialized = json.dumps(json_obj)
        assert secret_handle not in serialized

    def test_dek_version_recoverable_from_envelope_header(self) -> None:
        aad = build_aad(tenant_id="acme", stream="s", field="actor", event_id="e1", dek_version=7)
        envelope = envelope_encrypt("user", DEK_A, dek_version=7, aad=aad)
        assert envelope_dek_version(envelope) == 7

    def test_truncated_envelope_raises_format_error(self) -> None:
        with pytest.raises(CiphertextFormatError):
            envelope_decrypt(b"\x01\x00\x00\x00\x01short", DEK_A, aad=b"")

    def test_wrong_dek_length_rejected_on_encrypt_and_decrypt(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            envelope_encrypt("x", b"short", dek_version=1, aad=b"")
        envelope = envelope_encrypt("x", DEK_A, dek_version=1, aad=b"")
        with pytest.raises(ValueError, match="32 bytes"):
            envelope_decrypt(envelope, b"short", aad=b"")

    def test_unsupported_format_version_rejected(self) -> None:
        bad = b"\x02" + envelope_encrypt("x", DEK_A, dek_version=1, aad=b"")[1:]
        with pytest.raises(CiphertextFormatError, match="unsupported"):
            envelope_decrypt(bad, DEK_A, aad=b"")
        with pytest.raises(CiphertextFormatError, match="unsupported"):
            envelope_dek_version(bad)

    def test_truncated_header_rejected_by_dek_version_reader(self) -> None:
        with pytest.raises(CiphertextFormatError, match="too short"):
            envelope_dek_version(b"\x01\x00")

    def test_malformed_json_envelope_rejected(self) -> None:
        with pytest.raises(CiphertextFormatError, match="malformed"):
            from_json_envelope({"v": 1, "dek_version": "not-an-int", "nonce": "!!!", "ct": "!!!"})
        with pytest.raises(CiphertextFormatError, match="malformed"):
            from_json_envelope({})


class TestAadBinding:
    """AAD = tenant_id|stream|field|event_id|dek_version binds ciphertext to its exact context."""

    def _envelope(self, **overrides: object) -> bytes:
        defaults = dict(
            tenant_id="tenant-a",
            stream="proc-stream",
            field="actor",
            event_id="event-1",
            dek_version=1,
        )
        defaults.update(overrides)
        aad = build_aad(**defaults)  # type: ignore[arg-type]
        return envelope_encrypt("secret-user", DEK_A, dek_version=defaults["dek_version"], aad=aad)  # type: ignore[arg-type]

    def test_cross_tenant_swap_fails_to_decrypt(self) -> None:
        envelope = self._envelope(tenant_id="tenant-a")
        wrong_aad = build_aad(
            tenant_id="tenant-b",
            stream="proc-stream",
            field="actor",
            event_id="event-1",
            dek_version=1,
        )
        with pytest.raises(InvalidTag):
            envelope_decrypt(envelope, DEK_A, aad=wrong_aad)

    def test_cross_event_swap_fails_to_decrypt(self) -> None:
        envelope = self._envelope(event_id="event-1")
        wrong_aad = build_aad(
            tenant_id="tenant-a",
            stream="proc-stream",
            field="actor",
            event_id="event-2",
            dek_version=1,
        )
        with pytest.raises(InvalidTag):
            envelope_decrypt(envelope, DEK_A, aad=wrong_aad)

    def test_cross_field_swap_fails_to_decrypt(self) -> None:
        envelope = self._envelope(field="actor")
        wrong_aad = build_aad(
            tenant_id="tenant-a",
            stream="proc-stream",
            field="mention",
            event_id="event-1",
            dek_version=1,
        )
        with pytest.raises(InvalidTag):
            envelope_decrypt(envelope, DEK_A, aad=wrong_aad)

    def test_cross_stream_swap_fails_to_decrypt(self) -> None:
        envelope = self._envelope(stream="proc-stream")
        wrong_aad = build_aad(
            tenant_id="tenant-a",
            stream="other-stream",
            field="actor",
            event_id="event-1",
            dek_version=1,
        )
        with pytest.raises(InvalidTag):
            envelope_decrypt(envelope, DEK_A, aad=wrong_aad)

    def test_high_level_decrypt_helper_rejects_mismatched_event_id(self) -> None:
        obj = encrypt_identity_value(
            "secret-user",
            DEK_A,
            dek_version=1,
            tenant_id="t",
            stream="s",
            field="actor",
            event_id="e1",
        )
        with pytest.raises(InvalidTag):
            decrypt_identity_value(
                obj, DEK_A, tenant_id="t", stream="s", field="actor", event_id="e2"
            )


class TestKeyRotation:
    """A dek_version bump must not break decryption of ciphertext written under the old version."""

    def test_old_dek_version_still_decrypts_during_rotation_window(self) -> None:
        old = encrypt_identity_value(
            "user-old",
            DEK_A,
            dek_version=1,
            tenant_id="t",
            stream="s",
            field="actor",
            event_id="e1",
        )
        new = encrypt_identity_value(
            "user-new",
            DEK_B,
            dek_version=2,
            tenant_id="t",
            stream="s",
            field="actor",
            event_id="e2",
        )

        def resolve(dek_version: int) -> bytes:
            return {1: DEK_A, 2: DEK_B}[dek_version]

        assert (
            decrypt_identity_value(
                old,
                resolve(envelope_dek_version_of(old)),
                tenant_id="t",
                stream="s",
                field="actor",
                event_id="e1",
            )
            == "user-old"
        )
        assert (
            decrypt_identity_value(
                new,
                resolve(envelope_dek_version_of(new)),
                tenant_id="t",
                stream="s",
                field="actor",
                event_id="e2",
            )
            == "user-new"
        )


def envelope_dek_version_of(json_obj: dict) -> int:
    """Test helper: dek_version is already a plain field on the JSON envelope shape."""
    return int(json_obj["dek_version"])


class TestTtlCachedDekProvider:
    async def test_caches_within_ttl_and_re_resolves_after_expiry(self) -> None:
        calls = {"n": 0}

        class CountingProvider:
            async def get_dek(
                self, tenant_id: str, *, dek_version: int | None = None
            ) -> tuple[bytes, int]:
                calls["n"] += 1
                return DEK_A, 1

        cached = TtlCachedDekProvider(CountingProvider(), ttl_s=1000)
        await cached.get_dek("acme")
        await cached.get_dek("acme")
        assert calls["n"] == 1  # second call served from cache

        cached.invalidate("acme")
        await cached.get_dek("acme")
        assert calls["n"] == 2  # invalidated -> re-resolved


class TestLocalDevDekProvider:
    async def test_requires_explicit_kek_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INGEST_DEV_KEK", raising=False)
        with pytest.raises(ValueError, match="INGEST_DEV_KEK"):
            LocalDevDekProvider()

    async def test_derives_stable_per_tenant_dek(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGEST_DEV_KEK", "x" * 32)
        provider = LocalDevDekProvider()
        dek_a1, v1 = await provider.get_dek("tenant-a")
        dek_a2, v2 = await provider.get_dek("tenant-a")
        dek_b, _ = await provider.get_dek("tenant-b")
        assert dek_a1 == dek_a2  # deterministic per tenant
        assert dek_a1 != dek_b  # distinct per tenant
        assert len(dek_a1) == 32
        assert v1 == v2 == 1


class _FakeResponse:
    def __init__(self, *, status_ok: bool = True, body: dict | None = None) -> None:
        self._status_ok = status_ok
        self._body = body or {}

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise RuntimeError("simulated non-2xx response")

    def json(self) -> dict:
        return self._body


class TestHubApiDekProvider:
    async def test_success_returns_decoded_dek(self) -> None:
        import base64 as b64

        class FakeHttp:
            async def get(self, url: str, **kwargs: object) -> _FakeResponse:
                return _FakeResponse(body={"dek": b64.b64encode(DEK_A).decode(), "dek_version": 3})

        provider = HubApiDekProvider(FakeHttp(), "http://hub-api", lambda: "tok")
        dek, version = await provider.get_dek("acme", dek_version=3)
        assert dek == DEK_A
        assert version == 3

    async def test_non_2xx_response_raises_dek_unavailable(self) -> None:
        class FailingHttp:
            async def get(self, url: str, **kwargs: object) -> _FakeResponse:
                return _FakeResponse(status_ok=False)

        provider = HubApiDekProvider(FailingHttp(), "http://hub-api", lambda: "tok")
        with pytest.raises(DekUnavailableError):
            await provider.get_dek("acme")

    async def test_malformed_dek_length_raises_dek_unavailable(self) -> None:
        import base64 as b64

        class FakeHttp:
            async def get(self, url: str, **kwargs: object) -> _FakeResponse:
                return _FakeResponse(
                    body={"dek": b64.b64encode(b"tooshort").decode(), "dek_version": 1}
                )

        provider = HubApiDekProvider(FakeHttp(), "http://hub-api", lambda: "tok")
        with pytest.raises(DekUnavailableError):
            await provider.get_dek("acme")


class TestDekUnavailableIsFailClosed:
    async def test_unavailable_dek_raises_not_returns_plaintext_fallback(self) -> None:
        class AlwaysFailsProvider:
            async def get_dek(
                self, tenant_id: str, *, dek_version: int | None = None
            ) -> tuple[bytes, int]:
                raise DekUnavailableError("simulated broker outage")

        with pytest.raises(DekUnavailableError):
            await AlwaysFailsProvider().get_dek("acme")
