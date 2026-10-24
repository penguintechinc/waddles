"""The token vault: AES-256-GCM at rest, loud failure on every key problem, tamper detection."""

from __future__ import annotations

import base64

import pytest

from services.provider_connections.errors import VaultCorrupt, VaultUnavailable
from services.provider_connections.vault import PlatformKeyVault, TokenVault

AAD = b"7:twitch"


@pytest.fixture
def vault() -> PlatformKeyVault:
    return PlatformKeyVault()


def test_satisfies_the_vault_protocol(vault: PlatformKeyVault) -> None:
    assert isinstance(vault, TokenVault)


def test_round_trip_and_ciphertext_is_not_the_plaintext(vault: PlatformKeyVault) -> None:
    plaintext = "bot-access-token-0123456789"
    sealed = vault.seal(plaintext, aad=AAD)
    assert sealed != plaintext
    assert plaintext not in sealed
    assert plaintext.encode() not in base64.b64decode(sealed)
    assert vault.open(sealed, aad=AAD, seal_version=1) == plaintext


def test_every_seal_uses_a_fresh_iv(vault: PlatformKeyVault) -> None:
    assert vault.seal("same", aad=AAD) != vault.seal("same", aad=AAD)


def test_a_tampered_ciphertext_is_detected(vault: PlatformKeyVault) -> None:
    raw = bytearray(base64.b64decode(vault.seal("token", aad=AAD)))
    raw[-1] ^= 0x01
    with pytest.raises(VaultCorrupt):
        vault.open(base64.b64encode(bytes(raw)).decode(), aad=AAD, seal_version=1)


@pytest.mark.parametrize("garbage", ["", "not base64 !!", "AAAA"])
def test_garbage_is_corrupt_not_unavailable(vault: PlatformKeyVault, garbage: str) -> None:
    with pytest.raises(VaultCorrupt):
        vault.open(garbage, aad=AAD, seal_version=1)


def test_a_ciphertext_from_another_key_is_corrupt(
    vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = vault.seal("token", aad=AAD)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ef" * 32)
    with pytest.raises(VaultCorrupt):
        vault.open(sealed, aad=AAD, seal_version=1)


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
            vault.seal("token", aad=AAD)

    def test_open_refuses_without_a_usable_key_and_says_unavailable_not_corrupt(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sealed = vault.seal("token", aad=AAD)
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        with pytest.raises(VaultUnavailable):
            vault.open(sealed, aad=AAD, seal_version=1)

    def test_no_plaintext_fallback_exists(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        with pytest.raises(VaultUnavailable):
            vault.seal("token", aad=AAD)  # raises -- it never returns the plaintext "to keep going"

    def test_the_key_never_appears_in_the_error(
        self, vault: PlatformKeyVault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "zz" * 32)
        with pytest.raises(VaultUnavailable) as info:
            vault.seal("token", aad=AAD)
        assert "zz" * 32 not in info.value.message


class TestSealVersioning:
    """regression: M4 -- v1 is AAD-bound, v0 is the legacy no-AAD layout."""

    def test_v1_open_with_the_wrong_row_is_corrupt(self, vault: PlatformKeyVault) -> None:
        sealed = vault.seal("token", aad=b"7:twitch")
        with pytest.raises(VaultCorrupt):
            vault.open(sealed, aad=b"8:twitch", seal_version=1)

    def test_v1_open_without_aad_is_corrupt_never_a_silent_downgrade(
        self, vault: PlatformKeyVault
    ) -> None:
        sealed = vault.seal("token", aad=AAD)
        with pytest.raises(VaultCorrupt):
            vault.open(sealed, aad=None, seal_version=1)
        with pytest.raises(VaultCorrupt):
            vault.open(sealed, aad=None, seal_version=0)

    def test_vault_open_v0_ignores_aad(self, vault: PlatformKeyVault) -> None:
        from services.platform_integrations_crypto import encrypt_token

        legacy = encrypt_token("legacy-token")  # exactly what pre-M4 rows hold
        assert vault.open(legacy, aad=AAD, seal_version=0) == "legacy-token"
        assert vault.open(legacy, aad=None, seal_version=0) == "legacy-token"

    @pytest.mark.parametrize("version", [-1, 2, 99])
    def test_an_unknown_seal_version_is_refused(
        self, vault: PlatformKeyVault, version: int
    ) -> None:
        with pytest.raises(VaultCorrupt):
            vault.open(vault.seal("token", aad=AAD), aad=AAD, seal_version=version)
