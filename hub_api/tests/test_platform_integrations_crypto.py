"""Unit coverage for `services.platform_integrations_crypto` (SECURITY HIGH decrypt port).

Decrypt-only counterpart to `credential_manager_module/services/
test_token_crypto.py` -- both must agree on the same AES-256-GCM wire
format for `CREDENTIAL_ENCRYPTION_KEY`-derived ciphertext to round-trip
across the two services.
"""

from __future__ import annotations

import base64
import os
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    connection_aad,
    decrypt_if_needed,
    decrypt_value,
    encrypt_token,
    platform_connection_aad,
)

# Fixed test-only AES key, not a real credential.
_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


def _encrypt(plaintext: str) -> str:
    key = bytes.fromhex(_KEY)
    iv = os.urandom(12)
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv + ciphertext_and_tag).decode("ascii")


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


class TestDecryptValue:
    def test_decrypts_a_value_encrypted_with_the_same_key(self) -> None:
        assert decrypt_value(_encrypt("real-token")) == "real-token"

    def test_tampered_ciphertext_raises(self) -> None:
        ciphertext = _encrypt("token")
        tampered = ciphertext[:-4] + ("AAAA" if ciphertext[-4:] != "AAAA" else "BBBB")
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(tampered)

    def test_garbage_input_raises(self) -> None:
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value("not-valid-ciphertext")

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(_encrypt("token"))


class TestDecryptIfNeeded:
    def test_encrypted_true_decrypts(self) -> None:
        assert decrypt_if_needed(_encrypt("tok"), is_encrypted=True) == "tok"

    def test_encrypted_false_passes_through(self) -> None:
        assert decrypt_if_needed("plaintext-tok", is_encrypted=False) == "plaintext-tok"

    def test_none_passes_through(self) -> None:
        assert decrypt_if_needed(None, is_encrypted=True) is None

    def test_corrupt_encrypted_value_falls_back_to_raw(self) -> None:
        assert decrypt_if_needed("corrupt", is_encrypted=True) == "corrupt"


class TestAadBinding:
    """regression: M4 -- a ciphertext is bound to the row it was sealed for."""

    def test_aad_roundtrip(self) -> None:
        sealed = encrypt_token("tok", aad=b"7:twitch")
        assert decrypt_value(sealed, aad=b"7:twitch") == "tok"

    def test_wrong_aad_fails_closed(self) -> None:
        sealed = encrypt_token("tok", aad=b"7:twitch")
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(sealed, aad=b"8:twitch")

    def test_missing_aad_on_v1_fails_closed(self) -> None:
        sealed = encrypt_token("tok", aad=b"7:twitch")
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(sealed, aad=None)

    def test_aad_on_a_legacy_no_aad_ciphertext_fails_closed(self) -> None:
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(_encrypt("tok"), aad=b"7:twitch")

    def test_no_aad_wire_format_is_unchanged(self) -> None:
        assert decrypt_value(encrypt_token("tok")) == "tok"
        assert encrypt_token("tok", aad=b"x") != encrypt_token("tok", aad=b"x")

    def test_connection_aad_is_community_and_provider(self) -> None:
        assert connection_aad(community_id=7, provider="twitch") == b"7:twitch"

    def test_platform_connection_aad_is_public_id_and_platform(self) -> None:
        pid = UUID("12345678-1234-5678-1234-567812345678")
        assert (
            platform_connection_aad(public_id=pid, platform="discord")
            == b"conn:12345678-1234-5678-1234-567812345678:discord"
        )
