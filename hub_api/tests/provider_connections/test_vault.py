"""The token vault: AES-256-GCM at rest, loud failure on every key problem, tamper detection."""

from __future__ import annotations

import base64

import pytest

from services.provider_connections.errors import VaultCorrupt, VaultUnavailable
from services.provider_connections.vault import PlatformKeyVault, TokenVault


@pytest.fixture
def vault() -> PlatformKeyVault:
    return PlatformKeyVault()


def test_satisfies_the_vault_protocol(vault: PlatformKeyVault) -> None:
    assert isinstance(vault, TokenVault)


def test_round_trip_and_ciphertext_is_not_the_plaintext(vault: PlatformKeyVault) -> None:
    plaintext = "bot-access-token-0123456789"
    sealed = vault.seal(plaintext)
    assert sealed != plaintext
    assert plaintext not in sealed
    assert plaintext.encode() not in base64.b64decode(sealed)
    assert vault.open(sealed) == plaintext


def test_every_seal_uses_a_fresh_iv(vault: PlatformKeyVault) -> None:
    assert vault.seal("same") != vault.seal("same")


def test_a_tampered_ciphertext_is_detected(vault: PlatformKeyVault) -> None:
    raw = bytearray(base64.b64decode(vault.seal("token")))
    raw[-1] ^= 0x01
    with pytest.raises(VaultCorrupt):
        vault.open(base64.b64encode(bytes(raw)).decode())


@pytest.mark.parametrize("garbage", ["", "not base64 !!", "AAAA"])
def test_garbage_is_corrupt_not_unavailable(vault: PlatformKeyVault, garbage: str) -> None:
    with pytest.raises(VaultCorrupt):
        vault.open(garbage)


def test_a_ciphertext_from_another_key_is_corrupt(
    vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = vault.seal("token")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ef" * 32)
    with pytest.raises(VaultCorrupt):
        vault.open(sealed)


class TestKeyHandling:
    @pytest.mark.parametrize("bad", [None, "", "zz" * 32, "ab" * 31, "ab" * 33, "not hex"])
    def test_seal_refuses_without_a_usable_key(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch, bad: str | None
    ) -> None:
        if bad is None:
            monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        else:
            monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", bad)
        with pytest.raises(VaultUnavailable, match="CREDENTIAL_ENCRYPTION_KEY"):
            vault.seal("token")

    def test_open_refuses_without_a_usable_key_and_says_unavailable_not_corrupt(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sealed = vault.seal("token")
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        with pytest.raises(VaultUnavailable):
            vault.open(sealed)

    def test_no_plaintext_fallback_exists(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        with pytest.raises(VaultUnavailable):
            vault.seal("token")  # raises -- it never returns the plaintext "to keep going"

    def test_the_key_never_appears_in_the_error(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "zz" * 32)
        with pytest.raises(VaultUnavailable) as info:
            vault.seal("token")
        assert "zz" * 32 not in info.value.message
